---
area: deploy
accepted: by 2026-05-02
---
# ADR #32 — OpenTofu instead of Terraform + native S3 locking

**Context:** Terraform state locking required a DynamoDB table as an additional resource.

**Decision:** OpenTofu >= 1.7 with `use_lockfile = true` — native S3 locking via conditional writes, without DynamoDB.

**Reasons:**
- Single S3 bucket for everything (Terraform state + Pulumi state + locking)
- Fewer resources for the initial setup
- OpenTofu is open-source and actively maintained

**Consequences:** `provider.tf` updated, DynamoDB state lock table removed from documentation.

**Note:** Superseded by ADR #34 (AWS SAM replaces OpenTofu for shared infrastructure).
