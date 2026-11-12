# SQS + Lambda: break the flow, recover the message

Make Lambda fail, trace SQS retries, and recover a message from the DLQ. Our first local lab: real code, real logs, no AWS account required.

## Setup

- Native route: a supported Linux system (x86_64 or aarch64, glibc ≥ 2.17), Python 3.9 or newer with venv/pip, and a text editor. A Linux VM also works; Docker is not required for this route.
- Install AWS Emulator using the official Linux instructions below. macOS and Windows do not have a native emulator binary; use a supported Linux environment or the optional Docker route.
- The native setup uses two terminals and loopback port 19397. Keep the emulator running throughout the exercise. No AWS account, personal AWS credentials, or AWS CLI is required.
- Reference run: AWS Emulator 1.0.322 on Linux aarch64, validated inside a container from macOS. The native Linux installation itself was not tested on a separate host. Package installation and dependency download require internet access.

### 1. Clone the repository

```bash
git clone https://github.com/leodc/sqs-lambda-retries.git
cd sqs-lambda-retries
```

### 2. Linux, without Docker · terminal A

First install AWS Emulator for your architecture using the official instructions linked below. Then create the Python environment and start the emulator. Leave this terminal running. If your distribution separates python3-venv or pip into a package, install it first. Wait until the emulator reports that it is ready.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
aws-emulator --version
bash start-emulator.sh
```

https://docs.aws.amazon.com/emulator/latest/userguide/getting-started-linux.html

### 3. Linux · terminal B

In a second terminal, enter the cloned repository directory (adjust the path if needed) and activate its Python environment. Run the practice stages here.

```bash
cd sqs-lambda-retries
source .venv/bin/activate
```

### Alternative for macOS / Windows: Docker

Use this instead of the two Linux terminal steps. From the cloned repository directory, build the Linux image and enter its shell. It starts the emulator and includes Python and nano. Run every stage inside that shell. The running container has no external network, mounted AWS credentials, or published ports. Building the image needs internet access and a Docker engine configured for Linux containers.

```bash
docker build -t sqs-lambda-practice .
docker run --rm -it --name sqs-lambda-practice --network none --memory=2g --memory-swap=2g sqs-lambda-practice
```

## 1. Build the experiment, with consumption disabled

Create a source queue, its dead-letter queue, a Lambda function, and a disabled event source mapping. The helper uses only the emulator’s generated credentials and an explicit loopback endpoint.

```bash
python lab.py prepare
python lab.py inspect
```

- Find mapping state Disabled, consumer fail_poison true, and empty source/DLQ counts.
- Read configuration: batch size 1, function timeout 1 second, visibility timeout 6 seconds, max_receive_count 3. Inspect handler.py before proceeding.

Why start with the mapping disabled?

It gives you a stable point to inspect the infrastructure before Lambda polls the queue. No message has been sent yet.
The short settings make failure observable in a small exercise. AWS recommends a visibility timeout at least six times the function timeout and maxReceiveCount of at least five; three is a deliberate teaching choice, not a production recommendation.

## 2. Send two messages, then stop

Send order-ok and order-poison. Sending a message should not silently start the next stage.

```bash
python lab.py send
python lab.py inspect
```

- Find two sent records with different message IDs. Keep the ID for order-poison.
- The source should show two visible messages; the mapping is still Disabled and the DLQ is empty.

Why are there no handler logs yet?

An SQS message does not invoke the function on its own. The enabled event source mapping polls SQS and invokes Lambda. We have deliberately left that mapping disabled.
Repeated send calls are rejected for this session, so accidentally copying the block twice will not enqueue another pair.

## 3. Enable consumption and watch the failure

Start polling. Retries now proceed in the background, even while you read this page. You can run inspect again at any point to retrieve new logs.

```bash
python lab.py consume
python lab.py inspect
```

- Look for received and processed for order-ok, and deliberate_failure for order-poison. If logs have not arrived, inspect again.
- Follow message_id and receive_count. Queue counters can change as the consumer works; an empty visible count alone does not mean success.

What lets the failed message become available again?

The handler raises an error for order-poison. The message is not successfully acknowledged and deleted. After its visibility timeout it can be received again.
With batch size one, the good message is not retried alongside the failing one. Larger batches and partial batch responses are a separate experiment.

## 4. Inspect the retries and the DLQ

Wait for the failed message to reach the DLQ, then inspect its body and metadata. This command has a bounded wait; it does not repair the consumer or redrive the message.

```bash
python lab.py diagnose
python lab.py inspect
```

- Find three deliberate_failure logs for the same message, with receive_count 1, 2, and 3. That is three deliveries: the initial attempt plus two retries.
- Find a diagnosis event for order-poison, a successful order-ok log, and one visible message in the DLQ.
- The helper receives the DLQ message to inspect it, then immediately restores its visibility. That inspection itself increments the receive count.

Does a receive count of 4 prove that Lambda failed four times?

No. In the reference run, Lambda failed three times. The fourth receive was our diagnostic read from the DLQ, not another function invocation. Count the actual handler failure logs.
The source queue’s redrive policy moves the message to its DLQ. This is an SQS queue configuration, not the DLQ setting for asynchronous Lambda invocations.
Calling diagnose again returns the saved diagnosis instead of receiving the DLQ message again.

## 5. Fix the consumer, then inspect again

Open consumer.json and change fail_poison from true to false. In nano, save with Ctrl+O, Enter, and exit with Ctrl+X. Then apply the configuration change to the emulated function.

```bash
nano consumer.json
python lab.py repair
python lab.py inspect
```

- Confirm consumer fail_poison is false. Editing the file alone does not update the function; repair applies it.
- The DLQ should still contain one message. The function fix has not moved it back to the source queue.

Why has the fixed function not processed that message yet?

The mapping polls the source queue. The message is waiting in a different queue: the DLQ. Fixing the consumer and recovering failed messages are separate operations.
The helper rejects repair while fail_poison is still true. This makes the file edit an explicit part of the exercise.

## 6. Return the message to the source queue

Start a redrive task from the DLQ to the source queue only after inspecting the fix. The consumer remains enabled.

```bash
python lab.py redrive
python lab.py inspect
```

- Find redrive_started with a task handle. This confirms the request, not completed recovery.
- The message can already be processing by the time you inspect. Use the next stage to check the completed task, handler success, and both queues.

What would happen if you redrove the message before fixing the failure?

It would meet the same failing handler again. Redrive moves messages; it does not resolve the underlying error.
The lab’s stage guard prevents this ordering mistake. Production recovery needs the same reasoning even when the API allows the action.

## 7. Verify recovery with more than an empty queue

Wait for the redrive task to complete, a processed log for order-poison, and zero visible, in-flight, and delayed messages in both queues.

```bash
python lab.py verify
python lab.py inspect
```

- Find verification with result PASS, and the handler’s processed event for order-poison.
- Read redrive_identity_observed. In emulator 1.0.322 the ID and receive count were preserved. AWS documents new IDs and enqueue times after redrive: this is an observed fidelity gap.

Why would an empty DLQ be insufficient evidence?

A message could have moved without being processed successfully. The helper combines task completion, a successful handler log, and queue counts.
Use the emulator to study the flow, but do not turn its preserved message ID or exact retry timing into assumptions about AWS.

## 8. Keep the evidence and clean up

Your local observations are in .lab/events.jsonl, including the original Lambda log messages. Inspect and keep them before stopping the environment. Cleanup deletes this session’s emulated resources; it keeps the local evidence files.

```bash
python lab.py cleanup
tail -n 5 .lab/events.jsonl
```

- Find cleanup_completed. The saved resource state is removed; .lab/events.jsonl remains.
- Native Linux: stop terminal A with Ctrl+C. Docker: before exiting, copy .lab out from a host terminal using docker cp sqs-lambda-practice:/opt/lab/.lab ./practice-logs. Then exit the container shell.
- If the emulator was restarted mid-lab, its in-memory resources are gone. Run python lab.py reset, then prepare to start over. Logs are retained, so use timestamps to separate attempts.

What evidence would you use to explain this incident to someone else?

Keep the original message ID, the three failure log entries, the diagnostic DLQ read, the applied fix, the redrive task, and the final success. That tells a more useful story than a screenshot of an empty queue.
Do not publish the raw emulator startup log without reviewing it: it can contain generated local credentials. The events file does not include those credentials.

## Limits

- This is real execution against an emulator, not execution in an AWS account. It does not establish production IAM enforcement, quotas, scaling behavior, durability, or timing.
- The reference runtime is 1.0.322. New emulator versions can change behavior; the helper records the version and source hashes in its environment event.
- The exercise uses a standard queue, one record per invocation, and a deterministic failure. It does not test FIFO ordering, duplicate side effects, or partial batch responses.
- A Terraform plan checks proposed infrastructure changes. This exercise observes message processing and failure recovery. They answer different questions.
