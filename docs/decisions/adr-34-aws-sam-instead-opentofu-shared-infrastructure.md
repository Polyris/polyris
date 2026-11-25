---
area: deploy
accepted: by 2026-05-02
---
# ADR #34 — AWS SAM instead of OpenTofu for Shared Infrastructure

**Context:** OpenTofu required an external state backend (S3 bucket), DynamoDB or S3 locking, and a separate CLI tool.

**Decision:** AWS SAM (`sam build && sam deploy`) + CloudFormation for shared infrastructure.

**Reasons:**
- AWS CloudFormation manages state natively — zero external dependencies
- SAM packages Lambda ZIP and uploads to S3 automatically
- `samconfig.toml` — one place for all parameters
- Launch Stack URL is possible for OSS onboarding
- Fewer tools: OpenTofu removed, SAM = AWS-native

**What stayed unchanged:**
- SSM Parameters — CFN writes to the same `/polyris/{stage}/` paths
- `from_ssm()` in Pulumi — works without changes
- Pulumi — remained for pipeline deploys (separate refactoring step, see ADR #35)
