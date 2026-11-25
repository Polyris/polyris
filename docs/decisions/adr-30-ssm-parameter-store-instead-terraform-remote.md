# ADR #30 — SSM Parameter Store instead of Terraform Remote State for Pipeline Configuration

**Context:** Pulumi pipelines require ARNs of shared infrastructure (wrapper SFN, DynamoDB tables, IAM role).

**Previous approach:** `from_terraform_state()` — read S3 Terraform state file via IAM role assume. Required a dedicated IAM role, S3 access, and knowledge of bucket/key.

**Decision:** `from_ssm()` — Terraform automatically writes values to SSM Parameter Store under `/polyris/{stage}/` after `sam deploy`. Pulumi reads from there.

**Reasons:**
- Simpler UX: zero configuration for the user after `sam deploy`
- No separate IAM role needed for state reads
- SSM is the standard AWS pattern for cross-service configuration
- Transparent: values visible in AWS Console

**Consequences:** `from_terraform_state()`, `get_terraform_state()`, `TerraformState` — removed.
