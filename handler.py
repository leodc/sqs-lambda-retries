"""A deliberate failure to observe SQS retries, then repair and redrive."""

import json
import os


def lambda_handler(event, context):
    for record in event["Records"]:
        observation = {
            "message_id": record["messageId"],
            "body": record["body"],
            "receive_count": record["attributes"]["ApproximateReceiveCount"],
        }
        print(json.dumps({"event": "received", **observation}, sort_keys=True))
        if record["body"] == "order-poison" and os.environ.get("FAIL_POISON") == "true":
            print(json.dumps({"event": "deliberate_failure", **observation}, sort_keys=True))
            raise RuntimeError("Deliberate failure for order-poison")
        print(json.dumps({"event": "processed", **observation}, sort_keys=True))
    return {"ok": True}
