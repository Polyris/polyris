---
area: deploy
accepted: by 2026-11-25
revised: 2026-11-26
---
# ADR #140 — The orchestration role makes the direct steps' calls

**Context.** A pipeline's direct steps (`polyris.steps`: `SNSTask`, `SQSTask`, `S3Task`, `DynamoDBTask`, `EventBridgeTask`, `BedrockTask`, `HttpTask`) run in its state machine under the platform's orchestration role, on resources the pipeline names. The role allowed the task services (Lambda, Step Functions, ECS, Batch, Glue, Athena, EMR) on any resource, but no `sns:Publish`, `sqs:SendMessage` or `bedrock:InvokeModel`, and S3 and DynamoDB only on the platform's own bucket and tables: those steps failed with AccessDenied on a user's resources (the docs audit after 1.3). Principle #45 kept `sns:Publish` and `sqs:SendMessage` out of the template, so that the platform sends no message of its own.

**Decision.**

1. The orchestration role allows the direct steps' calls on any resource: `sns:Publish`, `sqs:SendMessage`, `bedrock:InvokeModel`, `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, and DynamoDB's `GetItem`, `PutItem`, `UpdateItem`, `DeleteItem`, `Query` and `Scan` (statement `AllowDirectSteps`), as it allows the task services' calls.
2. Principle #45 stands for the platform. Its guard (`tests/sdk/test_no_monitoring_infrastructure.py`) excepts this one statement, which holds the direct steps' calls and nothing else; the platform creates no topic or queue and publishes nothing of its own.
3. `tests/sdk/test_orchestration_role_direct_steps.py` holds every direct step class to a grant of what its integration calls.
4. The platform's own data that the role never uses stays denied to a step (`DenyDirectStepsOnPlatformData`, `DenyDirectStepsOnPlatformFiles`): the console's tokens, the task specs and the dependency pointers, the console's files and the release's artifacts. Without the deny, a `DynamoDBTask` could write the console an admin token and an `S3Task` the files the next platform update deploys.

**Alternatives.** A policy per pipeline on exactly the resources its steps and tasks name, written by `polyris-deploy`: least privilege, and a new IAM mechanism, left for later. Leaving each user to grant the calls: every user met AccessDenied first.

**Consequences.** The role can reach any topic, queue, model, bucket and table of the account. Only Step Functions of the account can assume it, and a deployer runs only the `*-polyris-*` stacks and state machines of the platform's deploy policy, never the platform's own (ADR #142 §2, §5), so who may deploy a pipeline decides what it touches. A team that keeps data away from it puts the platform in an account of its own and reaches the data with a task's `role=` (ADR #142 §10). A resource encrypted with a customer-managed KMS key still needs its key policy to allow the role.
