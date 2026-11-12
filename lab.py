"""A staged local experiment. Never connects to an AWS account."""

import argparse
import configparser
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent
WORK = ROOT / ".lab"
STATE = WORK / "state.json"
ENDPOINT = "https://127.0.0.1:19397"
EMULATOR = Path.home() / ".aws/emulator/19397"


def emit(event, **fields):
    row = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    line = json.dumps(row, sort_keys=True)
    print(line, flush=True)
    WORK.mkdir(exist_ok=True)
    with (WORK / "events.jsonl").open("a") as file:
        file.write(line + "\n")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def wait_for(label, read, accept, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = read()
        if accept(value):
            return value
        time.sleep(0.5)
    raise TimeoutError(f"{label}: not ready after {seconds}s. Run inspect; last response: {value}")


class Lab:
    def __init__(self):
        credential_file = EMULATOR / "emulator_credentials"
        require(credential_file.is_file(), "Start the emulator on port 19397 in another terminal first.")
        self.fingerprint = hashlib.sha256(credential_file.read_bytes()).hexdigest()
        # Replace the SDK environment before constructing clients: no host profiles or credentials.
        for key in list(os.environ):
            if key.startswith("AWS_"):
                del os.environ[key]
        os.environ.update({
            "AWS_CONFIG_FILE": str(EMULATOR / "config"),
            "AWS_SHARED_CREDENTIALS_FILE": str(credential_file),
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_CA_BUNDLE": str(EMULATOR / "ca.pem"),
        })
        credentials = configparser.ConfigParser()
        credentials.read(credential_file)
        local = credentials["default"]
        session = boto3.Session(
            aws_access_key_id=local["aws_access_key_id"],
            aws_secret_access_key=local["aws_secret_access_key"],
            region_name="us-east-1",
        )
        config = Config(connect_timeout=3, read_timeout=10,
                        retries={"total_max_attempts": 1}, inject_host_prefix=False)
        def client(name):
            return session.client(name, endpoint_url=ENDPOINT,
                                  verify=str(EMULATOR / "ca.pem"), config=config)
        require(client("sts").get_caller_identity()["Account"] == "000000000000",
                "The endpoint did not identify the emulator account.")
        self.sqs, self.lam, self.iam, self.logs = [client(name) for name in ("sqs", "lambda", "iam", "logs")]
        self.state = json.loads(STATE.read_text()) if STATE.exists() else {}
        require(not self.state or self.state["emulator"] == self.fingerprint,
                "The emulator was restarted. Its old resources are gone. Run: python lab.py reset")

    def save(self):
        WORK.mkdir(exist_ok=True)
        temporary = STATE.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(STATE)

    def phase(self, *allowed):
        require(self.state.get("phase") in allowed,
                f"Expected stage {allowed}; current: {self.state.get('phase', 'not prepared')}. Run inspect.")

    def set_phase(self, phase):
        self.state["phase"] = phase
        self.save()
        emit("stage_completed", stage=phase)

    def counts(self, url):
        response = self.sqs.get_queue_attributes(QueueUrl=url, AttributeNames=[
            "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible",
            "ApproximateNumberOfMessagesDelayed",
        ])
        return {key: int(value) for key, value in response["Attributes"].items()}

    def read_logs(self):
        if not self.state.get("function"):
            return []
        records, token = [], None
        for _ in range(100):
            try:
                response = self.logs.filter_log_events(logGroupName=self.state["group"],
                    **({"nextToken": token} if token else {}))
            except ClientError as error:
                if error.response["Error"]["Code"] == "ResourceNotFoundException":
                    return []
                raise
            records.extend(response.get("events", []))
            next_token = response.get("nextToken")
            if not next_token or next_token == token:
                break
            token = next_token
        seen = set(self.state.get("log_ids", []))
        observations = []
        for record in records:
            if record["eventId"] not in seen:
                emit("lambda_log", **record)
                seen.add(record["eventId"])
            try:
                observation = json.loads(record["message"])
                if isinstance(observation, dict):
                    observations.append(observation)
            except ValueError:
                pass
        self.state["log_ids"] = sorted(seen)
        self.save()
        return observations

    def prepare(self):
        require(not self.state, "A session already exists. Inspect it or run cleanup first.")
        name = "practice-sqs-" + uuid4().hex[:10]
        self.state = {"name": name, "group": "/aws/lambda/" + name,
                      "emulator": self.fingerprint, "phase": "preparing", "messages": {}}
        self.save()
        emit("environment", endpoint=ENDPOINT, boto3=boto3.__version__,
             emulator=subprocess.check_output(["aws-emulator", "--version"], text=True).strip(),
             lab_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             handler_sha256=hashlib.sha256((ROOT / "handler.py").read_bytes()).hexdigest())
        self.state["dlq"] = self.sqs.create_queue(QueueName=name + "-dlq",
            Attributes={"MessageRetentionPeriod": "1209600"})["QueueUrl"]
        self.save()
        self.state["dlq_arn"] = self.sqs.get_queue_attributes(QueueUrl=self.state["dlq"],
            AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        self.state["source"] = self.sqs.create_queue(QueueName=name, Attributes={
            "VisibilityTimeout": "6", "MessageRetentionPeriod": "86400",
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": self.state["dlq_arn"], "maxReceiveCount": 3}),
        })["QueueUrl"]
        self.save()
        self.state["source_arn"] = self.sqs.get_queue_attributes(QueueUrl=self.state["source"],
            AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        self.state["role"] = self.iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps({
            "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                "Principal": {"Service": "lambda.amazonaws.com"}}],
        }))["Role"]["Arn"]
        self.save()
        self.iam.put_role_policy(RoleName=name, PolicyName="consume-and-log", PolicyDocument=json.dumps({
            "Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
                 "Resource": self.state["source_arn"]},
                {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                 "Resource": "arn:aws:logs:us-east-1:000000000000:log-group:" + self.state["group"] + ":*"},
            ],
        }))
        self.state["policy"] = True
        self.save()
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w") as archive:
            archive.write(ROOT / "handler.py", "handler.py")
        self.lam.create_function(FunctionName=name, Runtime="python3.12", Role=self.state["role"],
            Handler="handler.lambda_handler", Code={"ZipFile": package.getvalue()}, Timeout=1,
            Environment={"Variables": {"FAIL_POISON": "true"}})
        self.state["function"] = name
        self.save()
        self.lam.get_waiter("function_active_v2").wait(FunctionName=name,
            WaiterConfig={"Delay": 1, "MaxAttempts": 30})
        self.state["mapping"] = self.lam.create_event_source_mapping(FunctionName=name,
            EventSourceArn=self.state["source_arn"], BatchSize=1, Enabled=False)["UUID"]
        self.save()
        (ROOT / "consumer.json").write_text('{\n  "fail_poison": true\n}\n')
        self.set_phase("prepared")
        self.inspect()

    def inspect(self):
        require(self.state, "Run prepare first.")
        self.read_logs()
        for label in ("source", "dlq"):
            if label in self.state:
                emit("queue", queue=label, counts=self.counts(self.state[label]))
        if self.state.get("mapping"):
            mapping = self.lam.get_event_source_mapping(UUID=self.state["mapping"])
            emit("mapping", state=mapping["State"], last_result=mapping.get("LastProcessingResult"))
        if self.state.get("function"):
            function = self.lam.get_function_configuration(FunctionName=self.state["function"])
            emit("consumer", fail_poison=function["Environment"]["Variables"]["FAIL_POISON"])
        emit("session", stage=self.state["phase"], configuration={
            "batch_size": 1, "function_timeout_seconds": 1,
            "visibility_timeout_seconds": 6, "max_receive_count": 3,
        })

    def send(self):
        self.phase("prepared")
        for body in ("order-ok", "order-poison"):
            if body not in self.state["messages"]:
                identifier = self.sqs.send_message(QueueUrl=self.state["source"], MessageBody=body)["MessageId"]
                self.state["messages"][body] = identifier
                self.save()
                emit("sent", body=body, message_id=identifier)
        self.set_phase("sent")

    def consume(self):
        self.phase("sent")
        self.lam.update_event_source_mapping(UUID=self.state["mapping"], Enabled=True)
        wait_for("enable mapping", lambda: self.lam.get_event_source_mapping(UUID=self.state["mapping"]),
                 lambda response: response["State"] == "Enabled")
        self.set_phase("consuming")

    def diagnose(self):
        self.phase("consuming", "diagnosed")
        if self.state["phase"] == "diagnosed":
            emit("diagnosis_already_saved", evidence=self.state["diagnosis"])
            return
        def observe():
            self.read_logs()
            return self.counts(self.state["dlq"])
        wait_for("failed message in DLQ", observe,
                 lambda counts: counts["ApproximateNumberOfMessages"] == 1)
        observations = self.read_logs()
        failures = [row for row in observations if row.get("event") == "deliberate_failure"]
        require([int(row["receive_count"]) for row in failures] == [1, 2, 3], "Expected three failed deliveries; inspect logs.")
        require(all(row["message_id"] == self.state["messages"]["order-poison"] for row in failures), "Unexpected failed message.")
        require(any(row.get("event") == "processed" and row.get("body") == "order-ok" for row in observations), "No successful control message.")
        require(not any(row.get("event") == "processed" and row.get("body") == "order-poison" for row in observations), "Poison message unexpectedly processed.")
        response = self.sqs.receive_message(QueueUrl=self.state["dlq"], MaxNumberOfMessages=1,
            VisibilityTimeout=30, MessageSystemAttributeNames=["All"])
        message = response["Messages"][0]
        try:
            require(message["Body"] == "order-poison" and message["MessageId"] == self.state["messages"]["order-poison"], "Unexpected DLQ message.")
            self.state["diagnosis"] = {"failed_deliveries": 3, "body": message["Body"],
                "message_id": message["MessageId"], "dlq_receive_count": message["Attributes"]["ApproximateReceiveCount"]}
            self.save()
        finally:
            self.sqs.change_message_visibility(QueueUrl=self.state["dlq"],
                ReceiptHandle=message["ReceiptHandle"], VisibilityTimeout=0)
        emit("diagnosis", **self.state["diagnosis"])
        self.set_phase("diagnosed")

    def repair(self):
        self.phase("diagnosed")
        settings = json.loads((ROOT / "consumer.json").read_text())
        require(settings.get("fail_poison") is False,
                "Edit consumer.json: set fail_poison to false, save, then run repair again.")
        self.lam.update_function_configuration(FunctionName=self.state["function"],
            Environment={"Variables": {"FAIL_POISON": "false"}})
        self.lam.get_waiter("function_updated_v2").wait(FunctionName=self.state["function"],
            WaiterConfig={"Delay": 1, "MaxAttempts": 30})
        self.set_phase("repaired")
        emit("message_still_in_dlq", counts=self.counts(self.state["dlq"]))

    def redrive(self):
        self.phase("repaired")
        result = self.sqs.start_message_move_task(SourceArn=self.state["dlq_arn"],
            DestinationArn=self.state["source_arn"], MaxNumberOfMessagesPerSecond=1)
        self.state["task"] = result["TaskHandle"]
        self.set_phase("redriving")
        emit("redrive_started", task_handle=result["TaskHandle"])

    def verify(self):
        self.phase("redriving", "verified")
        def move_status():
            return next((row for row in self.sqs.list_message_move_tasks(SourceArn=self.state["dlq_arn"])["Results"]
                         if row.get("DestinationArn") == self.state["source_arn"]), {})
        result = wait_for("redrive task", move_status,
            lambda row: row.get("Status") in ("COMPLETED", "FAILED", "CANCELLED"))
        require(result["Status"] == "COMPLETED", f"Redrive failed: {result}")
        observations = wait_for("handler recovery", self.read_logs, lambda rows:
            any(row.get("event") == "processed" and row.get("body") == "order-poison" for row in rows))
        for label in ("source", "dlq"):
            wait_for(label + " empty", lambda: self.counts(self.state[label]), lambda counts: sum(counts.values()) == 0)
        recovered = next(row for row in observations if row.get("event") == "processed" and row.get("body") == "order-poison")
        emit("redrive_identity_observed", preserved_id=recovered["message_id"] == self.state["messages"]["order-poison"],
             receive_count=recovered["receive_count"], note="AWS documents new IDs after redrive; preserved IDs are an emulator fidelity gap.")
        self.set_phase("verified")
        emit("verification", result="PASS", source=self.counts(self.state["source"]), dlq=self.counts(self.state["dlq"]))

    def cleanup(self):
        require(self.state, "No saved lab session to clean up.")
        def remove(operation, **parameters):
            try:
                operation(**parameters)
            except ClientError as error:
                if error.response["Error"]["Code"] not in (
                    "ResourceNotFoundException", "NoSuchEntity", "AWS.SimpleQueueService.NonExistentQueue", "QueueDoesNotExist"
                ):
                    raise
        if self.state.get("mapping"):
            remove(self.lam.delete_event_source_mapping, UUID=self.state["mapping"])
            def mapping_gone():
                try:
                    self.lam.get_event_source_mapping(UUID=self.state["mapping"])
                    return False
                except ClientError as error:
                    if error.response["Error"]["Code"] == "ResourceNotFoundException":
                        return True
                    raise
            wait_for("mapping deleted", mapping_gone, bool)
        if self.state.get("function"):
            remove(self.lam.delete_function, FunctionName=self.state["function"])
        for key in ("source", "dlq"):
            if self.state.get(key):
                remove(self.sqs.delete_queue, QueueUrl=self.state[key])
        if self.state.get("role"):
            if self.state.get("policy"):
                remove(self.iam.delete_role_policy, RoleName=self.state["name"], PolicyName="consume-and-log")
            remove(self.iam.delete_role, RoleName=self.state["name"])
        remove(self.logs.delete_log_group, logGroupName=self.state["group"])
        STATE.unlink()
        emit("cleanup_completed", logs=str(WORK), next="Stop the emulator with Ctrl+C in its terminal.")


def reset():
    require(STATE.exists(), "No saved session to reset.")
    state = json.loads(STATE.read_text())
    credentials = EMULATOR / "emulator_credentials"
    require(not credentials.exists() or hashlib.sha256(credentials.read_bytes()).hexdigest() != state["emulator"],
            "This session's emulator is still running. Use cleanup, not reset.")
    STATE.unlink()
    emit("stale_session_reset", note="No resources changed. Logs retained.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["prepare", "send", "consume", "inspect", "diagnose",
                                         "repair", "redrive", "verify", "cleanup", "reset"])
    args = parser.parse_args()
    try:
        if args.stage == "reset":
            reset()
        else:
            getattr(Lab(), args.stage)()
    except Exception as error:
        emit("error", error_type=type(error).__name__, message=str(error))
        sys.exit(1)
