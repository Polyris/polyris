# ADR #132 — Billing tags: one `polyris:` set on every resource AWS can tag

**Context.** Each kind of resource had its own tags. The Lambda functions had five (`Environment`, `Stack`, `Stage`, `Namespace`, `ManagedBy`), the tables and buckets three to five, and the 13 state machines, 17 log groups and most IAM roles none. A pipeline's resources had `Pipeline`, `Stage`, `Namespace`, `ManagedBy` and `Name`, its scheduler role only two of them. Nothing checked them, so a cost report filtered by tag missed most of the platform, and an IAM condition on `aws:ResourceTag/Namespace` matched only some resources.

**Decision.** Every resource that CloudFormation can tag carries the same keys, and no others:

| Key | Value |
|---|---|
| `polyris:namespace` | the `Namespace` parameter; for a pipeline, the namespace it is deployed with |
| `polyris:stage` | the `Stage` parameter; for a pipeline, the stage it is deployed with |
| `polyris:component` | `platform` in the platform stack, `pipeline` in a pipeline's stack |
| `polyris:pipeline` | the pipeline's id; pipeline resources only |

The old tags are removed. The stack tag `polyris:platform=true` stays: it is how `polyris-up` finds its stack (ADR #131), not a billing tag.

**Key design choices.**

*Prefixed keys.* Companies often have their own `Stage`, `Environment` or `Owner` tags. A `polyris:` key never collides with them and names its owner, so no `ManagedBy` tag is needed.

*Tags in the templates, not on the stack.* CloudFormation copies stack tags to resources, but only on stacks deployed with them: `polyris-up` could set them, a `sam deploy` from a checkout would not. Tags in the templates reach every stack and can be checked before a deploy. The Lambda functions take them from `Globals`; SAM has no `Globals` tags for state machines, so every other resource spells them. `polyris/resource_tags.py` holds the keys and builds a pipeline's tags.

*The rule follows AWS.* `tests/sdk/test_resource_tags.py` reads from the CloudFormation resource schemas that cfn-lint ships which types can be tagged and in which property (`tagging.taggable`, `tagging.tagProperty`). It checks every resource of the platform template after the SAM transform and of each pipeline template shape. Types AWS cannot tag, such as `AWS::Scheduler::Schedule` and `AWS::IAM::ManagedPolicy`, need no list of exceptions, and a type AWS learns to tag joins the rule when cfn-lint is updated. The test also fails if changing a type's tags would replace the resource (Principle #33).

*An HTTP API takes its tags from its OpenAPI body.* SAM writes the `ConsoleApi` tags into the body (`x-amazon-apigateway-tag-value`) and onto its stage; the test reads both.

**Consequences.**
- Cost Explorer can split the bill by installation, stage and component once the keys are activated as cost allocation tags in the paying account (`docs/how-to/manage-the-platform.md`).
- Most orchestration cost is state transitions of the platform's shared state machines (the wrapper, `run_task`), tagged `platform`. `polyris:pipeline` covers a pipeline's own state machine, log group and scheduler role, so tags cannot split the shared cost by pipeline.
- Upgrading removes the old tags: IAM conditions and cost reports on them stop matching. A pipeline's resources keep theirs until its next `polyris-deploy`.
- Users cannot add tags of their own through polyris; no user has asked for it.
