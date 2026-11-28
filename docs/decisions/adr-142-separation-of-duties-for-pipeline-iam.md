---
area: deploy
accepted: 2026-11-26
amends: "#131, #132, #140"
---
# ADR #142 — Separation of duties: authors deploy pipelines under a platform policy and create no IAM

## Context

Until 1.3.2 whoever deployed a pipeline also wrote IAM. The deploy permissions the docs gave as JSON allowed `iam:CreateRole`, `PutRolePolicy` and `UpdateAssumeRolePolicy` on `*-polyris-*-scheduler-role` and `iam:PassRole` of that name, so an author could create a role of any trust and policy under that name and pass it: an IAM escalation. Each scheduled pipeline's stack made its own scheduler role, which is why the deploy needed those actions. The orchestration role could be assumed by EventBridge, EventBridge Scheduler and API Gateway, could assume any role of its own account, read any secret, invoke the platform's internal functions, start `run_task` and `bulk_backfill` with input it chose, and change any EventBridge rule. The console started any state machine whose name was close to a pipeline's. `start-run`, `notify` and `ui-bootstrap` acted on fields of their event. The default task role could not run an EMR step and could write the platform's files.

## Decision

### 1. Two people, and only one of them writes IAM

The infrastructure engineer installs and updates the platform with `polyris-up` and creates every IAM role. The pipeline author deploys with `polyris-deploy` and holds no IAM write action, apart from the migration grant (§7). The author's workflow stays "write `dag.py`, run `polyris-deploy`"; the infrastructure engineer attaches one managed policy once.

### 2. The deploy policy is a resource of the platform stack

`${Namespace}-${Stage}-polyris-deploy-policy` (`AWS::IAM::ManagedPolicy`, output `PipelineDeployPolicyArn`) holds what `polyris-deploy` needs. It changes with the platform, so no release asks users to edit a policy by hand. It allows CloudFormation, Step Functions and log groups on `*-polyris-*` and denies them on the platform's own stack (`${AWS::StackId}`) and on the platform's state machines of any platform, `CreateStateMachine` included. It names the platform's own tables and results prefix exactly. It sets up the log delivery of a pipeline's state machine (`logs:*LogDelivery`, and the log groups' resource policy), which AWS asks of whoever creates a state machine that logs (`test_the_author_may_set_up_the_log_delivery_of_a_pipelines_state_machine`). Its name and `Description` never change (Principle #33). A `polyris-deploy` refused `iam:PassRole` names the policy to attach. `tests/sdk/test_deploy_policy.py`.

### 3. A role an author may pass is named, with the one service it goes to

The deploy policy passes two roles: the orchestration role to `states.amazonaws.com`, the schedule role to `scheduler.amazonaws.com` (`test_each_role_the_author_may_pass_goes_to_one_service`). The roles a pipeline runs as pass a role only to `ecs-tasks.amazonaws.com`, which the ECS task and step need (`test_the_roles_a_pipeline_runs_as_pass_a_role_to_ecs_tasks_alone`).

### 4. One schedule role, exported

Pipeline stacks hold no IAM resource, and `polyris-deploy` asks for no IAM capability. Every scheduled pipeline's schedule starts its state machine with the platform's `${Namespace}-${Stage}-polyris-schedule-role`, trusted only by EventBridge Scheduler of the account, allowed to start `*-polyris-*` and denied the platform's machines. The platform exports its ARN as `${Namespace}-${Stage}-polyris-schedule-role-arn`, and each scheduled pipeline stack imports it (`Fn::ImportValue`), so CloudFormation keeps the role while a pipeline uses it. The required-outputs check asks for the output only for a scheduled pipeline. The schedule's name, `{pipeline stack}-schedule`, takes the 64-character limit the stack's scheduler role had: namespace, stage and `dag_id` together at most 45 characters (`tests/sdk/test_platform_iam.py`, `tests/sdk/test_schedule_rule_state.py`, `tests/sdk/test_deploy_validation_gate.py`).

### 5. The roles a pipeline runs as are fenced off from the platform

The orchestration role is trusted by Step Functions of its account alone. It and the default task role never invoke the platform's internal functions, never start, stop or redrive `run_task` or `bulk_backfill`, and never reach the platform's tokens, specs, dependency pointers, console files, release artifacts or pipeline templates, of any platform in the account. The orchestration role reads only EventBridge connections' secrets (`events!connection/*`) and changes only the managed rules of the `.sync` integrations (`rule/StepFunctions*`).

The engine's roles (the dispatcher, the sweeper, AssetFanout) stop no execution of a platform machine but `run_task`. The console role starts `*-polyris-*` and never `run_task`, and Run starts only a registered pipeline.

`start-run` refuses an execution that is not one of its pipeline's registered state machine, and one whose registered machine is named like a platform's. `notify` takes a pipeline's channels and their config from the registry and rebuilds a failure from the task row the event's execution names, which must be the event's pipeline's and, for an alert, wait for a decision: an alert's task and date are the row's, and no link or config comes from the event. The event still chooses which of its pipeline's waiting rows an alert names, and its error text (§10). `ui-bootstrap` takes what it acts on from its environment.

What holds is these denials and checks: a direct step of a pipeline can still write the tokens table (run rows included), the registry, `asset-events`, `task-events` and `asset-subscriptions`, since `DenyDirectStepsOnPlatformData` covers only `api-tokens`, `pipeline-specs` and `dep-subscriptions` (§10). Every deny list and the reserved `dag_id` suffixes come from one constant, `platform.PLATFORM_NAMES`, walked to the template by `tests/sdk/test_platform_names.py`.

### 6. Same-account roles fail closed, except under `/polyris-tasks/`

The orchestration role denies `sts:AssumeRole` on every role of its account (`StringEqualsIfExists` on `aws:ResourceAccount`, so a request without the key is denied) except the default task role and `role/polyris-tasks/*`. A task's `role=` names a role of another account or a role of the platform's account under that path; a role gets its path when it is created, so only whoever creates IAM roles can put one there. `polyris-validate` (with `account_id`) and `polyris-deploy` (always) refuse any other role of the platform's account (`tests/sdk/test_task_roles.py`).

### 7. The migration grant is time-limited and guarded

For 1.4 only, the deploy policy passes `*-polyris-*-scheduler-role` to `scheduler.amazonaws.com` and may get, list and delete those roles and their inline policies: a pipeline's first redeploy deletes its old scheduler role, and a failed one rolls its schedule back to it. It is safe because `polyris-up` refuses to update while any role of that name is not the form `polyris-deploy` made (path `/`, trusted by the scheduler alone, one inline policy starting its own stack's state machine, no managed policy, held by a `*-polyris-*` stack). Each scheduled pipeline is redeployed on 1.4, before 1.5 removes the grant. `test_the_migration_grant_goes_in_1_5` fails from 1.5.0 until both statements go, with the code that serves them: the preflight's scan of scheduler roles and its rules, and the notice of the update into 1.4.

### 8. `polyris-up` checks before it changes anything, and never goes below 1.4

Before any upload it refuses a platform below 1.3 (ADR #137, *Migration and upgrade*), a task of a deployed pipeline whose role this platform refuses (§6, read from each pipeline's current spec version), and a planted scheduler role (§7). No release below 1.4 replaces 1.4 or newer, whatever the flags (`platform.rolls_back_below_floor`); while a scheduled pipeline imports the export of §4, CloudFormation holds the same line for an older `polyris-up`.

A new platform's stack name may not contain `-polyris-`, the form of a pipeline's stack. After an update that crosses into 1.4, `polyris-up` prints what the authors need, in order: attach the deploy policy, move to the 1.4 CLI, detach the old policy, run `polyris-up` once more, and redeploy each scheduled pipeline before 1.5 (§7).

`polyris-down` refuses while a stack imports the export. Every artifact is uploaded on every run, the template under a key nobody can predict, and a change set whose template is not the one uploaded is deleted (`cfn_changeset.verify_change_set_template`); `polyris-deploy` checks its uploaded template the same way.

### 9. Every work state of `run_task` has its actions on the default task role

`tests/sdk/test_run_task_work_iam.py` holds each work state's calls, EMR's `AddJobFlowSteps`, `DescribeStep` and `CancelSteps` among them, to a grant on the default task role. The role creates and ends no EMR cluster.

### 10. One platform is one trust domain; isolation is an account

The shared pipeline role orchestrates the account's workloads and data, so an author reaches any Glue job, Batch job, ECS task, EMR cluster, Lambda and state machine of the account, the account's data through the direct steps, every EventBridge connection through `HttpTask`, the CloudWatch Logs resource policies of the region (§2), any role of another account that trusts this one, and any role under `/polyris-tasks/`. Authors of one platform can disturb each other's pipelines and the platform's run rows, registry and facts: denial of service or confusion, never IAM escalation. Every `*-polyris-*` pattern spans the account, so two platforms in one account do not keep their authors apart; isolation is a separate account. `IAM_PERMISSIONS.md` states this residual.

The registry is one of the rows a pipeline's steps can write: the console's Run, a push start of `asset-fanout`, its retry by the sweeper and `bulk_backfill` start the state machine a pipeline's registry entry names, and only `start-run` refuses one named like a platform's. A rewritten entry can start `bulk_backfill` or a test state machine with a pipeline's input, which fails. The console may start no `run_task`, but `asset-fanout` and the sweeper may, since their passes of the dispatcher start it: a rewritten entry of a pipeline scheduled on an asset makes them start `run_task` with that pipeline's run input. `run_task` runs as the orchestration role, which the pipeline's own steps already hold, so it reaches nothing those steps do not. Closing one table to a pipeline's steps would leave the others open, so isolation stays a separate platform in its own account.

### 11. What the release's live smoke verifies

The tests check the template and the code; these are AWS's behaviour, checked in a dev account before the 1.4.0 tag:

- the `sts:AssumeRole` deny's `StringEqualsIfExists` on `aws:ResourceAccount`: a same-account role outside `/polyris-tasks/` is denied, one under it and one of another account are assumed;
- a pipeline's first redeploy on 1.4 deletes its old scheduler role under the deploy policy alone, with no IAM capability, and the IAM actions CloudFormation then calls (`iam:DeleteRole`, `DeleteRolePolicy`, `GetRole`, `ListRolePolicies`, `ListAttachedRolePolicies`, and any other it needs) are the migration grant's;
- `Fn::ImportValue` of the schedule role's export needs no permission of the caller beyond the deploy policy;
- whether the deploy policy's `logs:PutResourcePolicy` and log-delivery actions can be narrowed from `log-group:*` and `*`;
- that the scheduler roles of 1.3 carry no `aws:cloudformation:stack-id` tag: `polyris-up` finds the stack that holds one in the region of the state machine its policy starts, and depends on no tag;
- the deletion of the deploy policy at `polyris-down` while authors' roles hold it, and the installer actions it needs.

## Consequences

- Breaking for users: the role that runs `polyris-deploy` needs the new policy, a same-account `role=` outside `/polyris-tasks/` is refused, a 1.4 `polyris-deploy` cannot schedule a pipeline on a 1.3 platform, a 1.3 `polyris-deploy` under the new policy cannot create its scheduler role, a rule or API that reused the orchestration role needs its own role, and no platform goes back below 1.4. The CHANGELOG and `manage-the-platform.md` give the order that avoids each.
- A role under `/polyris-tasks/` is reachable by every pipeline of its platform: the path decides which roles pipelines may use, not which pipeline.
- An allowlist of the accounts `role=` may name is not built (Principle #18).
- ADR #140: the orchestration role is assumable by Step Functions of its account alone, and a deployer runs only `*-polyris-*` stacks and state machines, never the platform's. ADR #131: `polyris-up` checks before any change, uploads every artifact and verifies its template. ADR #132: a pipeline's resources are its state machine, log group and schedule.

## Alternatives

- **A role per pipeline**, written by `polyris-deploy`: least privilege per pipeline, and an author who writes IAM, which this decision removes. Rejected, as Airflow (MWAA), Dagster and Prefect also run pipelines under one shared role.
- **A permissions boundary with `iam:CreateRole` for authors:** a boundary policy every author's role must carry and every created role must have; one mistake in the boundary or its condition is an escalation, and each new task type widens it. Rejected for the named roles of §3.
- **A CloudFormation service role for pipeline stacks:** the stack's rights move from the author to a role CloudFormation assumes, but the author can pass that role to any stack of the name pattern, so the reach stays and the policy is harder to read.
- **ABAC on resource tags:** the tasks' resources are the user's own, untagged by polyris, and an author who can tag a resource could grant themselves access.
