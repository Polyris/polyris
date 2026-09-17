# 16 — XCom showcase

**How-to guide.** How to deploy and run this example to see every 1.0.0 XCom
reader / writer path exercised in one pipeline. For the API specification, see
[`docs/features/DATA_PASSING.md`](../../docs/features/DATA_PASSING.md); for the
design rationale, see
[ADR #123](../../docs/reference/adr-123-xcom-reliable-data-passing.md).

## Prerequisites

- polyris SAM stack deployed in the target AWS account (this example depends
  on its managed policies and DDB tables).
- `polyris >= 1.0.0` installed locally: `pip install polyris==1.0.0`.
- Credentials for the target AWS account in `AWS_PROFILE` or default chain.

## Pipeline shape

```
extract_dict ─────┐
extract_primitive ─┼──▶ report   (trigger_rule="all_done")
aggregate_glue ───┘
```

- `extract_dict` — Lambda returning a dict (baseline).
- `extract_primitive` — Lambda returning `42` (the primitive-value path).
- `aggregate_glue` — Glue job. Ships with two script variants:
  - `aggregate_with_push.py` — calls `xcom.push()`; real data reaches downstream.
  - `aggregate_no_push.py` — skips `xcom.push()`; downstream receives Glue's
    `{"JobRunId": ...}` API response and the Console shows the AWS-metadata
    warning banner.
- `report` — Lambda reading all three upstreams via `xcom.get()`.

## Files

```
16_xcom_showcase/
├── dag.py                              # the DAG — deploy with polyris-deploy
├── README.md                           # this file
├── lambda_handlers/
│   ├── extract_dict.py                 # returns a dict
│   ├── extract_primitive.py            # returns 42 (try [1,2,3], None, True, or raise)
│   └── report.py                       # uses xcom.get() with typed error opt-outs
└── glue_scripts/
    ├── aggregate_with_push.py          # calls xcom.push()
    └── aggregate_no_push.py            # skips xcom.push()
```

## Step 1 — provision AWS resources

Two options:

### Option A — reuse the shared test-resources.yaml

`examples/testing-infra/test-resources.yaml` already provisions a generic
`polyris-test-lambda` and `polyris-test-glue`. Edit `dag.py` to point every
`function_name=` at `polyris-test-lambda` and set
`job_name="polyris-test-glue"`. You lose the "different return shape per
producer" contrast but the `xcom.push()` demo still works if you upload
`aggregate_with_push.py` as the Glue script.

### Option B — dedicated resources (recommended for a clean demo)

Create three Lambdas (`polyris-xcom-extract-dict`,
`polyris-xcom-extract-primitive`, `polyris-xcom-report`) and one Glue job
(`polyris-xcom-aggregate`). Paste the files from `lambda_handlers/` and
`glue_scripts/` respectively.

The `report` Lambda's deployment zip must include `polyris>=1.0.0`
(pip install into the deployment directory).

The Glue job needs `polyris>=1.0.0` on the Spark cluster:

- Glue Job details → Job parameters →
  `--additional-python-modules` = `polyris==1.0.0`

## Step 2 — attach IAM

Attach the polyris-published managed policies to each function / job role:

| Role | `PolyrisTaskReadPolicy` | `PolyrisTaskWritePolicy` |
|------|:-----------------------:|:------------------------:|
| `polyris-xcom-extract-dict`      | not needed (returns via `return`) | not needed |
| `polyris-xcom-extract-primitive` | not needed (returns via `return`) | not needed |
| `polyris-xcom-report`            | **required** (calls `xcom.get()`) | not needed |
| `polyris-xcom-aggregate` (Glue role) | not needed for this demo | **required** (calls `xcom.push()`) |

CFN import syntax:

```yaml
ManagedPolicyArns:
  - !ImportValue myorg-dev-polyris-task-read-policy
  - !ImportValue myorg-dev-polyris-task-write-policy   # Glue role only
```

Replace `myorg-dev` with your polyris SAM stack's `${Namespace}-${Stage}`.
Full IAM contract:
[`docs/features/DATA_PASSING.md#iam`](../../docs/features/DATA_PASSING.md#iam).

## Step 3 — deploy

```bash
cd examples/16_xcom_showcase

# Local validation first — no AWS calls.
polyris-validate -v

# See the generated Step Functions definition (JSON).
polyris-output --json | jq .

# Deploy to AWS (uses the profile in polyris config).
polyris-deploy
```

## Step 4 — trigger a run and open the Console

Trigger the pipeline (Console → Pipelines → `xcom-showcase` → Run, or via
whichever scheduler you use). Then open the run and walk the four inspection
points below.

### 4a. `extract_dict` — baseline

Task Detail → **Output** tab: clean JSON, no banner:

```
{ "rows": 1240, "path": "s3://.../events.parquet" }
```

### 4b. `extract_primitive` — the primitive return path

Task Detail → **Output** tab: clean JSON, e.g. `42` (or whatever return value
you picked in `extract_primitive.py`).

To exercise the failure path, edit the handler to `raise` instead of returning:

- Task Detail → **Details** tab shows `status: failed`.
- Downstream (`report`) → **Input** tab shows a red banner for this dep:
  `extract_primitive — status: failed`.

### 4c. `aggregate_glue` — the metadata-leak comparison

Upload `aggregate_with_push.py` first:

- Task Detail → **Output** tab: clean JSON with your pushed dict, no banner.

Then swap to `aggregate_no_push.py` on the same Glue job and trigger another
run:

- Task Detail → **Output** tab: yellow warning banner at the top —
  > This output is an AWS API response, not application data. For
  > Glue/ECS/Batch tasks, call `xcom.push(value)` in your job code…
- Below it, the raw `{"JobRunId": "...", "StartedOn": "..."}` from Glue.

### 4d. `report` — the split-record Console preview

Task Detail → **Input** tab:

- **Variables** section — the pipeline's `variables=` merged with
  polyris-injected date variables.
- **Upstream (3)** section — three colored cards, one per upstream:
  - `extract_dict` — green, summary + expandable JSON payload.
  - `extract_primitive` — green if it returned; yellow "no output recorded" if
    it raised (but note that `report` still ran because
    `trigger_rule="all_done"`).
  - `aggregate_glue` — green if Glue script pushed; still green with the
    AWS-metadata JSON inside if it didn't (the metadata-warning banner shows
    up on `aggregate_glue`'s own Output tab, not here).

Task Detail → **Output** tab: the dict returned by `report.py`, e.g.

```
{"dict_rows": 1240, "primitive_value": 42, "glue_total": 500}
```

## Cleanup

```bash
polyris-deploy --destroy     # removes the pipeline stack
# Delete the three Lambdas and one Glue job you created in Step 1 if unused.
```

## Related

- API specification: [`docs/features/DATA_PASSING.md`](../../docs/features/DATA_PASSING.md)
- Design rationale: [`docs/reference/adr-123-xcom-reliable-data-passing.md`](../../docs/reference/adr-123-xcom-reliable-data-passing.md)
- `CHANGELOG.md` — 1.0.0 entry lists every added / fixed / breaking-risk item.
