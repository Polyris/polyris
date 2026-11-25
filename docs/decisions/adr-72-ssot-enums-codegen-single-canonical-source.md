# ADR #72 — SSoT enums codegen — single canonical source for status/rule enums (v0.79.0)

#### Context

Prior to v0.79.0, the same enum families lived as multiple hand-written
copies across the codebase (audit per v0.78.12 conversation):

| Family | Copies | Locations |
|---|---|---|
| TaskStatus | 4 | SDK + 2 Lambda + frontend |
| TriggerRule | 3 | SDK + 2 Lambda |
| BackfillStatus | 2 | console_api + frontend |
| BackfillCascade | 2 | console_api + frontend |
| BackfillGranularity | 2 | console_api + frontend |
| ExecutionStatus | 2 | (added v0.78.14) backend + frontend |
| PipelineStatus | 2 | console_api + frontend (loose) |
| StalenessStatus | 2 | both sides compute |

8 families, total 18+ copies. The existing `make sync-constants` only
checked backend↔backend; frontend drift was not caught. Real bugs from
this shape:
- ExecutionStatus mixed-case mess (closed by ADR #71 v0.78.14)
- "Active" → "Running" rename had to be coordinated in three places
  (closed by ADR #70 v0.78.13)
- A new enum value required editing 4 files and praying nothing was
  missed

This ADR establishes a single canonical source with a code generator.

#### Decision

**1. Canonical source: `polyris/constants.py`** (extended). Existing
`TaskStatus` (Enum), `TriggerRule` (class), and the various status sets
remain. Added (v0.79.0):
- `PipelineStatus`, `ExecutionStatus`, `BackfillStatus`, `BackfillCascade`,
  `BackfillGranularity`, `StalenessStatus`, `AssetOperator`
- `EXECUTION_STATUS_CANONICAL`, `BACKFILL_TERMINAL_STATUSES`,
  `BACKFILL_ACTIVE_STATUSES`
- `normalize_execution_status` (lifted from v0.78.14 Lambda code)

**2. Generator module: `polyris/codegen/sync_enums.py`**. Imports the
canonical Python module (not regex — handles Enum subclasses, derived
sets, inheritance), writes:
- `sam/lambdas/_shared/constants_generated.py` (Lambda shared, Python)
- `sam/lambdas/console_api/constants_generated.py` (Lambda, Python)
- `ui/src/generated/enums.ts` (TypeScript)

Each generated file has a banner `DO NOT EDIT — generated` with the
canonical SHA (first 16 chars). Idempotent: re-running with no source
changes produces identical bytes.

**3. CI drift gate: `make check-generate-enums`** (calls
`python -m polyris.codegen.sync_enums --check`). Exits 1 if any generated
file would change. CI runs this; PR can't merge if the developer edited
canonical without regenerating.

**4. Frontend fully migrated.** `ui/src/types/index.ts` no longer
defines `TaskStatus`/`TriggerRule`/`PipelineStatus`/`ExecutionStatus`/
`BackfillStatus`/`BackfillCascade`/`BackfillGranularity`/`StalenessStatus`
inline — re-exports them from `@/generated/enums`. All frontend code
uses the canonical types via this re-export.

**5. Backend pragmatic.** `sam/lambdas/console_api/constants.py` keeps
its hand-written enum classes (because they use class-level sets like
`TaskStatus.TERMINAL` at 8 sites). The generated module
`constants_generated.py` exists alongside. A unit test suite
`tests/test_enum_drift.py` (5 tests) asserts hand-written values agree
with canonical. Future v0.79.x will migrate the 8 sites and let the
hand-written disappear.

#### Why Python-import the canonical (not regex/AST-parse)

The canonical mixes `Enum` subclasses (TaskStatus), plain classes
(TriggerRule), and derived sets (`TERMINAL_STATUSES = {TaskStatus.SUCCESS.value, ...}`).
Re-implementing all that resolution in a regex/AST is a 2nd canonical
to drift from. Python's import machinery already does it correctly —
the generator just `from polyris import constants as src` and reads
the resolved attributes.

#### Why class-level (not enum.Enum) for generated Python

Lambda code expects bare strings: `if status == TaskStatus.RUNNING:`.
Using `enum.Enum` would force `.value` access. Class-level string
attributes match existing patterns and require no caller changes.

#### What this does NOT do (yet)

- **Does not migrate backend hand-written `TaskStatus` class to
  generated.** Class-level sets (`TaskStatus.TERMINAL`) used at 8
  sites. Migration is incremental — drift test catches divergence
  meanwhile.
- **Does not generate SFN templates.** Those embed status values
  ('running', 'SUCCEEDED' for SFN-internal Choice rules) but they're
  Jsonata-templated and would need a different generator. BACKLOG.
- **Does not generate from non-Python canonicals.** If JSON Schema or
  similar becomes the source for some types, that's another generator.

#### Tests

- `tests/test_normalize_execution_status.py` — already added v0.78.14
- `tests/test_enum_drift.py` (NEW, 5 tests):
  - TaskStatus backend values ⊆ canonical values
  - TriggerRule backend values == canonical values (strict)
  - BackfillStatus backend values == canonical values (strict)
  - Generator output in sync with canonical (re-runs `check_all()`)
  - `normalize_execution_status` behavior matches between backend
    copy and canonical (8 input cases tested)

#### CI integration

`.github/workflows/ci.yml` should call `make check-generate-enums`
(not changed in this PR — add when CI is touched next).
For local: `make generate-enums` to regenerate, or
`python -m polyris.codegen.sync_enums`.

#### Lesson

When multiple copies of a value drift, the fix isn't a stricter review
discipline ("remember to update all 4 files!") — it's removing the
multiplicity. Codegen does this: edit one, regenerate, files match by
construction. The drift test then becomes the contract enforcement
mechanism, not human attention.
