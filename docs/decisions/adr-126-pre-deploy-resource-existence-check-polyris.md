---
area: deploy
accepted: by 2026-11-12
---
# ADR #126 — Pre-deploy resource existence check in `polyris-deploy`

**Context.** `polyris-deploy` compiles a DAG into a CloudFormation template and deploys it. If a referenced AWS resource — an SFN state machine ARN, a Lambda function name, a Glue job, an ECS cluster or task definition, a Batch job queue or job definition, an Athena workgroup — does not exist, CloudFormation succeeds but every pipeline execution fails at runtime with a confusing AWS error that is hard to trace back to the missing resource. The problem is diagnosed hours or days after the deploy, not at deploy time.

**Decision.** Add a pre-deploy resource existence check step to `deploy_pipeline`, implemented in `polyris/resource_check.py`. The check scans `dag.tasks`, collects all external resource references, and verifies each one against the relevant AWS API before the CloudFormation template is generated. A confirmed-missing resource aborts the deploy with a clear error message; an unverifiable resource (IAM denied, throttled, transient outage) produces a warning and deploy proceeds.

**Key design choices.**

*Abort on confirmed missing, warn on cannot-verify.* An access-denied or throttle error is not evidence that the resource is absent — it is evidence that the check could not complete. Aborting on these would produce false-positive failures whenever the deploy role lacks describe permissions or the service is momentarily throttled. The two code sets are kept explicitly separate: `_CANNOT_VERIFY_CODES` routes to warning; `_ACCESS_DENIED_CODES` (a strict subset) additionally gates the IAM action hint so throttle errors never tell the operator to change their IAM policy.

*ECS and Batch use status-based existence checks; SFN/Lambda/Glue/Athena do not.* ECS and Batch APIs return objects with lifecycle status fields (`INACTIVE`, `DELETING`, `DELETED`, `INVALID`) rather than raising exceptions for resources that have been deregistered or deleted. The check explicitly filters these states. SFN, Lambda, Glue, and Athena raise an exception when the resource is absent; no status filtering is needed. This asymmetry is an AWS API design difference, not inconsistency in the check.

*Cross-account tasks (`task.role != "same"`) are reported, not checked.* The deploy session cannot assume a different IAM role, so checking cross-account resources would always produce access-denied warnings. The operator is informed via a distinct warning section so they know what was not verified.

*CloudFormation substitution variables (`${...}`) are silently skipped.* These are resolved at CFN deploy time by CloudFormation parameter substitution. Attempting to verify them before deploy would always fail.

*`dag.steps` excluded in V1.* Steps can reference SFN ARNs and Lambda names (`LambdaInvokeStep`, `StartExecutionStep`). The step-type surface is not yet stable enough to cover reliably. Added as a documented TODO in `resource_check.py`.

*`emr` tasks excluded by design.* `emr_cluster_id` is a runtime cluster ID that is typically created fresh per-run. Checking it at deploy time would produce false positives for every DAG that creates its own cluster.

*Runs on `--dry-run`; skipped on `--destroy`.* The check uses the AWS session already established for the dry run — the same session that reads STS identity and SAM stack outputs. Skipping it on `--destroy` is correct: no pipeline is being deployed, so there are no pipeline resources to verify. Destroy only needs the stack name.

**IAM requirements.** Full confidence requires `states:DescribeStateMachine`, `lambda:GetFunction`, `glue:GetJob`, `ecs:DescribeClusters`, `ecs:DescribeTaskDefinition`, `batch:DescribeJobQueues`, `batch:DescribeJobDefinitions`, `athena:GetWorkGroup` on the deploy role. Access-denied for any of these converts to a warning, not an abort.

**Consequences.**
- `deploy_pipeline` makes additional AWS API calls per unique task reference. For large DAGs the check adds latency proportional to the number of distinct external resources. Deduplication (same ARN referenced by multiple tasks = one API call) limits the blast for typical pipelines. Bulk deploys run the check per-DAG independently; the same resource in N DAGs triggers N checks.
- `polyris/resource_check.py` is fully covered at 100% branch coverage and independently testable without touching `deploy.py`.
- `_ACCESS_DENIED_CODES ⊂ _CANNOT_VERIFY_CODES` invariant is enforced by `test_access_denied_codes_is_strict_subset_of_cannot_verify_codes`. Adding a new error code requires asking: "is the fix to add an IAM permission?" — if yes, both sets; if no (throttle, KMS state, transient), `_CANNOT_VERIFY_CODES` only. Rule documented in `polyris/CLAUDE.md`.
- `test_every_task_type_has_checker_or_skip` enforces completeness: adding a new `TaskTypeLiteral` without updating `resource_check.py` fails the test immediately.
