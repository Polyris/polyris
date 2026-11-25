# ADR #9 — Pulumi for Pipelines, Terraform for Shared

> **SUPERSEDED — Pulumi removed.** Pipelines now deploy via `polyris-deploy`
> (CloudFormation); shared infra via AWS SAM. There is no Pulumi path anymore
> (`polyris/pulumi.py` deleted, no `import pulumi` in the codebase). See
> CLAUDE.md rules #34–#35 and the "Pulumi removed" CHANGELOG entry. Kept below
> as a historical record only.

**Decision:** Use different IaC tools for different layers.

**Rationale:**
- Pulumi: Python-native, good for pipelines
- Terraform: Battle-tested, good for infra
- Clear separation of concerns

**Shared (Terraform):** DynamoDB, Lambdas, helpers, console
**Pipelines (Pulumi):** Individual Step Functions, schedules
