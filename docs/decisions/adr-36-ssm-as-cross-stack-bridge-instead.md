---
area: deploy
accepted: by 2026-05-02
---
# ADR #36 — SSM as a Cross-Stack Bridge (instead of `Fn::ImportValue`)

**Context:** Shared infra (SAM) and pipeline stacks need a way to pass ARNs between each other.

**Decision:** AWS SSM Parameter Store — `/polyris/{stage}/wrapper_arn` and similar.

**Why not `Fn::ImportValue`:**
- SSM is unified across all tools — Pulumi, polyris-deploy, CDK, boto3
- `Fn::ImportValue` ties to CFN — if a customer wants different IaC, it requires a rewrite
- SSM is human-readable: `aws ssm get-parameter --name /polyris/dev/wrapper_arn`

**The single downside of SSM vs ImportValue:**
`Fn::ImportValue` protects against deletion of a shared stack while dependent pipeline stacks exist.
SSM does not provide that protection — it must be controlled by process.

**If migrating fully to CFN (without Pulumi) in the future:**
SSM can be replaced with `Fn::ImportValue`. The SAM template already contains all the necessary Exports:

```yaml
# shared infra stack (already in sam/template.yaml)
Outputs:
  DependencyWrapperArn:
    Export:
      Name: !Sub "${Namespace}-${Stage}-polyris-wrapper-arn"
  OrchestrationRoleArn:
    Export:
      Name: !Sub "${Namespace}-${Stage}-polyris-orchestration-role-arn"

# pipeline stack — instead of reading SSM:
Resources:
  PipelineStateMachine:
    Properties:
      RoleArn: !ImportValue myorg-dev-polyris-orchestration-role-arn
      DefinitionSubstitutions:
        wrapper_arn: !ImportValue myorg-dev-polyris-wrapper-arn
```

CFN will additionally protect against an accidental `sam delete` while dependent pipeline stacks exist.
