# ADR #35 — `polyris-deploy` as a Parallel Alternative to Pulumi

**Context:** Pulumi requires a separate CLI, account, and state backend. For new users this is an additional barrier.

**Decision:** `polyris-deploy` — a built-in CLI command that deploys pipelines via CloudFormation.

**What it does:**
1. Reads `dag.py` (the same file Pulumi reads)
2. Reads SSM `/polyris/{stage}/` (the same parameters)
3. Generates a CFN template (SFN + LogGroup + EventBridge)
4. `aws cloudformation deploy` — AWS manages state
5. Registers the pipeline via boto3

**What stayed unchanged:** *(Historical: at the time, Pulumi remained as an
equal second option.)* **Update — Pulumi was later removed entirely;
`polyris-deploy` (CFN) is now the only pipeline-deploy path.** See CLAUDE.md
#34–#35.

**Advantages of the CFN approach:** native AWS state, rollback, zero external dependencies.
