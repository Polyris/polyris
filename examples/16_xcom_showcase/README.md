# 16 — XCom showcase

Every 0.100.0 XCom reader/writer path exercised in one pipeline, so you can
open the Console after a run and see every new visual and semantic in place.

## What this demonstrates

- **`xcom.get(event, task)`** uniform reader — one API for every task type
- **`xcom.push(value)`** writer for service tasks (Glue) — closes the
  "service tasks store {JobRunId} instead of data" metadata leak
- **Primitive return values** (`42`, `[1,2,3]`, `None`, `True`) — used to be
  wrapped in `{"_raw": "..."}` before 0.100.0
- **Loud errors by default** — `XComMissingError`, `XComUpstreamFailedError`,
  `XComTruncatedError`; opt out with `raise_on_missing=False` /
  `raise_on_failure=False`
- **`all_done` trigger + tolerant reads** — realistic pattern for fan-in
  consumers that need to handle partial upstream failure
- **Console UI** — colored per-upstream cards, AWS-metadata warning banner,
  one-time 0.100.0 onboarding banner

## Pipeline shape

```
extract_dict ─────┐
extract_primitive ─┼──▶ report   (trigger_rule="all_done")
aggregate_glue ───┘
```

- `extract_dict` — Lambda returning a dict (baseline).
- `extract_primitive` — Lambda returning `42` (the case that used to break).
- `aggregate_glue` — Glue job. Ships with two script variants:
  - `aggregate_with_push.py` — calls `xcom.push()`, real data reaches downstream.
  - `aggregate_no_push.py` — deliberately doesn't push, so Console shows the
    AWS-metadata warning banner. Swap between the two to see the contrast.
- `report` — Lambda reading all three via `xcom.get()`.

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
    ├── aggregate_with_push.py          # calls xcom.push() — the fix
    └── aggregate_no_push.py            # skips push() — the metadata trap
```

## One-time AWS setup

Before deploying the DAG, create the referenced Lambda functions and Glue
job in your AWS account. You have two options:

### Option A — reuse the shared test-resources.yaml

`examples/testing-infra/test-resources.yaml` already provisions a generic
`polyris-test-lambda` and `polyris-test-glue`. Edit `dag.py` to point every
`function_name=` at `polyris-test-lambda` and set `job_name="polyris-test-glue"`.
You lose the "different return shape per producer" contrast but the
`xcom.push()` demo still works if you upload `aggregate_with_push.py` as the
Glue script.

### Option B — dedicated resources (recommended for a clean demo)

Create three Lambdas (`polyris-xcom-extract-dict`, `polyris-xcom-extract-primitive`,
`polyris-xcom-report`) and one Glue job (`polyris-xcom-aggregate`). Paste the
files from `lambda_handlers/` and `glue_scripts/` respectively.

The `report` Lambda's deployment zip must include `polyris>=0.100.0`
(pip install into the deployment directory).

The Glue job needs `polyris>=0.100.0` on the Spark cluster:

- Glue Job details → Job parameters →
  `--additional-python-modules` = `polyris==0.100.0`

### Required IAM

Attach the polyris-published managed policies to each function/job role:

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

## Deploy

```bash
cd examples/16_xcom_showcase

# Local validation first — no AWS calls.
polyris-validate -v

# See the generated Step Functions definition (JSON).
polyris-output --json | jq .

# Deploy to AWS (uses the profile in polyris config).
polyris-deploy
```

## What to look at in the Console after a run

Open the Console, find the `xcom-showcase` pipeline, click into the latest run.

### 1. `extract_dict` — normal producer, sanity check

Task Detail → **Output** tab:

- Clean JSON, no banner:
  ```
  { "rows": 1240, "path": "s3://.../events.parquet" }
  ```

### 2. `extract_primitive` — the primitive fix in action

Task Detail → **Output** tab:

- Clean JSON: `42` (or whatever return value you picked in
  `extract_primitive.py`).
- Pre-0.100.0 this would render as `{"_raw": "42"}` in the Task Detail Input
  tab of any downstream task. Now it renders as `42` there too.

If you set the handler to `raise` instead:

- Task Detail → **Details** tab shows `status: failed`.
- Downstream (`report`) → **Input** tab shows a **red banner** for this dep:
  `extract_primitive — status: failed`.

### 3. `aggregate_glue` — the metadata-leak demo

Upload `aggregate_with_push.py` first:

- Task Detail → **Output** tab: clean JSON with your pushed dict, no banner.

Then swap to `aggregate_no_push.py` on the same Glue job and trigger another
run:

- Task Detail → **Output** tab: **yellow warning banner** at the top:
  > This output is an AWS API response, not application data. For
  > Glue/ECS/Batch tasks, call `xcom.push(value)` in your job code…
- Below it, the raw `{"JobRunId": "...", "StartedOn": "..."}` from Glue.

This is the metadata-leak trap 0.100.0's Console banners surface — the leak
existed for years before the banner made it visible.

### 4. `report` — the split-record Console preview

Task Detail → **Input** tab:

- **Variables** section (bordered code block) — the pipeline's `variables=`
  merged with polyris-injected date variables.
- **Upstream (3)** section — three colored cards, one per upstream:
  - `extract_dict` — **green** ▶ summary + expandable JSON payload
  - `extract_primitive` — **green** if it returned; **yellow** "no output
    recorded" if it raised (but note that `report` still ran because
    `trigger_rule="all_done"`)
  - `aggregate_glue` — **green** if Glue script pushed; still green with the
    AWS-metadata JSON inside if it didn't (the metadata-warning banner shows
    up on `aggregate_glue`'s own Output tab, not here)

Task Detail → **Output** tab:

- The dict returned by `report.py`:
  ```
  {"dict_rows": 1240, "primitive_value": 42, "glue_total": 500}
  ```

### 5. One-time onboarding banner

First time you open the Output tab (any task) after upgrading to 0.100.0,
you'll see a dismissible banner at the top:

> **Updated in 0.100.0:** upstream deps now render as colored cards with
> actionable messages — click the summary to expand raw data. …

Click Dismiss — the banner remembers your choice per browser via
localStorage (`polyris.ui.taskDetailBannerDismissed_v100`). Cleared
automatically in polyris 0.102.0.

## Related

- Full docs: [`docs/features/DATA_PASSING.md`](../../docs/features/DATA_PASSING.md)
- Design rationale: [`docs/reference/adr-123-xcom-reliable-data-passing.md`](../../docs/reference/adr-123-xcom-reliable-data-passing.md)
- `CHANGELOG.md` — 0.100.0 entry lists every added / fixed / breaking-risk item.
