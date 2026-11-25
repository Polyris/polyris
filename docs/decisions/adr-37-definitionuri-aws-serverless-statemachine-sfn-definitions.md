# ADR #37 — `DefinitionUri` + `AWS::Serverless::StateMachine` for SFN Definitions

**Context:** SFN definitions were originally inline in `template.yaml` as `DefinitionString`. This led to a problem: editing `sfn_templates/*.tpl.json` files had no effect — `sam deploy` did not see changes because it compared the hash of `template.yaml` (which was unchanged).

**Decision:** Convert all 13 SFN resources from `AWS::StepFunctions::StateMachine` + `DefinitionString` to `AWS::Serverless::StateMachine` + `DefinitionUri`.

**How it works:**
- `sam build` reads each `DefinitionUri` file and inlines it as `DefinitionString` in `.aws-sam/build/template.yaml`
- `sam package` (for releases) replaces local paths with S3 URLs — compatible with Tier 1 Launch Stack
- `DefinitionSubstitutions` works with `DefinitionUri` without changes

**Syntax changes** (`AWS::StepFunctions` → `AWS::Serverless`):
- `StateMachineName` → `Name`
- `StateMachineType` → `Type`
- `RoleArn` → `Role`
- `LoggingConfiguration` → `Logging`

**Exceptions:** `TestQuickSfn`, `TestSuccessSfn`, `TestFailureSfn` — remained
`AWS::StepFunctions::StateMachine` with inline `Definition` (simple single-step SFNs, no separate files).

**Result:** `sfn_templates/*.tpl.json` — single place for editing SFN definitions.
Edit the file → `sam build && sam deploy` → deployed.
