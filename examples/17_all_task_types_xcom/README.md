# 17 — All task types × XCom (end-to-end smoke)

**How-to guide.** How to deploy and run this pipeline to verify every polyris
1.0.0 task type participates in XCom bidirectionally. For the API spec, see
[`docs/features/DATA_PASSING.md`](../../docs/features/DATA_PASSING.md).

Companion narrower demo of just the reader/writer surface:
[`examples/16_xcom_showcase/`](../16_xcom_showcase/). This one is broader —
it exercises **every task type** in a single chain, so a run through the
Console proves each hop's xcom I/O works.

## Pipeline shape

Strict linear chain — makes cross-hop data flow easy to trace in the Console:

```
seed_lambda           (Lambda, returns dict → xcom natively)
  → transform_spark   (Glue ETL Spark, xcom.push)
  → aggregate_pyshell (Glue pythonshell, xcom.push)
  → summary_athena    (Athena SQL — see Athena note)
  → resolve_athena    (Lambda, unpacks QueryExecutionId, returns real value)
  → compute_ecs       (ECS Fargate, xcom.push)
  → render_batch      (Batch Fargate, xcom.push)
  → publish_sfn       (nested Step Functions, native Output)
```

Every task reads its upstream via xcom **and** writes its output via xcom.
The chain terminates at `publish_sfn`; its Output contains the final
`published_url` derived from every previous hop.

## What's excluded and why

- **EMR** — `xcom.push()` is not supported for EMR in 1.0.0 (see
  [DATA_PASSING.md](../../docs/features/DATA_PASSING.md#emr-and-athena--reads-yes-xcompush-no)).
  A pipeline requiring "every task fully participates" cannot include EMR.

## Athena's asymmetric role

Athena SQL can't call `xcom.push()` (no Python hook) and can't call
`xcom.get()` at runtime (SQL is not the polyris runtime). Its xcom
participation is:

- **Read side** — none at runtime. Bake constants into the query at
  DAG-definition time with a Python f-string (see `SILVER_THRESHOLD` in
  `dag.py`). polyris does NOT template `query_string` — a `{{ ... }}` or
  `{% ... %}` placeholder reaches Athena as literal characters and breaks
  the SQL parser. The SDK guard fails this at `polyris-validate` since
  1.0.1. `variables=` on the DAG records values for Console visibility
  but is NOT interpolated into the query.
- **Write side** — the wrapper stores the `StartQueryExecution` API response
  (`{"QueryExecution": {"QueryExecutionId": "..."}}`) as the task's xcom
  output. That's AWS metadata, not data.
- **Handoff** — the following Lambda (`resolve_athena`) reads that metadata,
  calls `boto3.athena.get_query_results`, and returns the real value. This
  is the documented Lambda-after-Athena pattern.

The Console banner will flag `summary_athena`'s output as AWS metadata — that
is expected and correct for Athena in 1.0.0.

## Files

```
17_all_task_types_xcom/
├── dag.py                              # the DAG — deploy with polyris-deploy
├── README.md
├── lambda_handlers/
│   ├── seed.py                         # returns dict
│   └── resolve_athena.py               # unpacks QueryExecutionId
├── glue_scripts/
│   ├── transform_spark.py              # Glue ETL / Spark, xcom.push
│   └── aggregate_pyshell.py            # Glue pythonshell, xcom.push
├── ecs_task_code/
│   └── compute.py                      # ECS container xcom.push
├── batch_task_code/
│   └── render.py                       # Batch container xcom.push
└── sfn_definitions/
    └── publish.json                    # nested SM Output = task's xcom
```

## Prerequisites

- polyris SAM stack deployed in the target AWS account (this pipeline uses
  `PolyrisTaskReadPolicy` + `PolyrisTaskWritePolicy` published by that stack).
- `polyris >= 1.0.0` installed locally.
- The eight AWS resources this DAG references. The shared
  `examples/testing-infra/test-resources.yaml` provisions a subset of these
  under `polyris-test-*` names; the rest need to be added — see
  [Provisioning the resources](#provisioning-the-resources) below.

## Provisioning the resources

Existing (already in `testing-infra/test-resources.yaml`):

| Resource | Name |
|----------|------|
| ECS cluster | `polyris-test-ecs` |
| Batch queue | `polyris-test-queue` |
| Athena workgroup | `polyris-test-wg` |
| Analytics database | `analytics` |
| Shared S3 bucket | `polyris-test-<account>-<region>` |
| VPC / subnets / SG | (from testing-infra) |

New (add to `testing-infra/test-resources.yaml` or a companion stack):

| Task | Resource | Notes |
|------|----------|-------|
| `seed_lambda` | Lambda `polyris-test-xcom-all-seed` | inline handler — see `lambda_handlers/seed.py`; no SDK |
| `transform_spark` | Glue Spark job `polyris-test-xcom-all-transform-spark` | `Command.Name = glueetl`, `GlueVersion = 5.0` (Python 3.11, matches polyris `requires-python`), `DefaultArguments: --extra-py-files: s3://…/polyris-1.0.0-*.whl`, PolyrisTaskRead/Write policies |
| `aggregate_pyshell` | Glue pythonshell job `polyris-test-xcom-all-aggregate-pyshell` | `Command.Name = pythonshell`, `DefaultArguments: --extra-py-files: s3://…/polyris-1.0.0-*.whl`, PolyrisTaskRead/Write |
| `summary_athena` | Table `analytics.silver_stats` with column `silver_count` | pre-populated for the demo; the query is deterministic |
| `resolve_athena` | Lambda `polyris-test-xcom-all-resolve-athena` | `polyris>=1.0.0` in zip, PolyrisTaskRead + `athena:GetQueryResults` + `s3:GetObject` on workgroup output |
| `compute_ecs` | ECS TaskDefinition `polyris-test-xcom-all-compute` | Python-capable image (see `ecs_task_code/compute.py` docstring for Dockerfile sketch), PolyrisTaskRead/Write on the task role |
| `render_batch` | Batch JobDefinition `polyris-test-xcom-all-render` | Python-capable image (same Dockerfile pattern as ECS), PolyrisTaskRead/Write |
| `publish_sfn` | Nested state machine `polyris-test-xcom-all-publish` | definition from `sfn_definitions/publish.json` |

Every task's IAM contract is documented in its handler file's docstring.

## Deploy

```bash
cd examples/17_all_task_types_xcom

# Local validation first — no AWS calls.
polyris-validate -v

# Inspect the compiled Step Functions definition.
polyris-output --json | jq .

# Deploy to AWS.
polyris-deploy
```

## What to look at in the Console after a run

Open the run and click through each task's **Output** tab (top-to-bottom in
the DAG panel). Every card should be green with a small dict as the value:

1. `seed_lambda` → `{"batch_id", "rows", "s3_seed"}`
2. `transform_spark` → `{"bronze_rows", "s3_bronze"}` — Green. If yellow with
   `{"JobRunId": ...}`, the Spark job didn't call `xcom.push()` (or the SDK
   isn't installed on the cluster).
3. `aggregate_pyshell` → `{"silver_count", "s3_silver"}` — same as #2.
4. `summary_athena` → **yellow banner "AWS API response"** with
   `{"QueryExecution": {"QueryExecutionId": ...}}` — expected; see the
   Athena note above.
5. `resolve_athena` → `{"verified_count"}` — Green. If missing, check the
   Lambda logs for `athena:GetQueryResults` permission errors.
6. `compute_ecs` → `{"metrics": {"score", "source"}}`.
7. `render_batch` → `{"report_path", "score"}`.
8. `publish_sfn` → `{"published_url", "score"}` — the final chain output.

Every task's **Input** tab should show the previous task's Output under
`upstream.<task>.output`. The Console's per-upstream card grammar (1.0.0)
renders each hop as one row with a green stripe.

## This is a smoke test — not a production pattern

The ECS / Batch containers in this example use a **runtime install** pattern:
`python:3.12` base image → `pip install 'polyris @ git+https://github.com/Polyris/polyris@v1.0.0'`
at container start → run inline `xcom.get()` / `xcom.push()` code. This is
fine for smoke-testing that the wrapper injects `POLYRIS_*` env vars and
`xcom.push()` reaches DDB end-to-end, but it has multiple runtime failure
points that make it unsuitable for production:

- Depends on GitHub availability at every container start.
- Adds ~15-20 s cold-start latency per task run (pip install + git clone).
- Shell heredoc quoting: `<<'PYCODE'` (single-quoted marker) is required to
  prevent shell interpolation of the Python code; unquoted `<<PYCODE` breaks.
- `python:3.12` image tag drifts silently over time; the polyris SDK version
  in the pinned git ref must stay in sync with the container's Python runtime.
- Untested for private repos (would need SSH deploy key or `x-access-token`
  URL scheme).

**For a real deployment**, bake the polyris SDK into a pre-built container
image, push it to ECR, and set the image on the ECS TaskDefinition /
Batch JobDefinition. The container's `CMD` becomes your actual task code —
no pip install, no download, no bootstrap layer.

Sketch:

```dockerfile
FROM python:3.12-slim
RUN pip install --no-cache-dir polyris==1.0.0
COPY compute.py /app/compute.py
CMD ["python", "/app/compute.py"]
```

```bash
docker build -t <your-account>.dkr.ecr.<region>.amazonaws.com/polyris-compute:1.0.0 .
aws ecr get-login-password ... | docker login ...
docker push <your-account>.dkr.ecr.<region>.amazonaws.com/polyris-compute:1.0.0
```

Then point `AWS::ECS::TaskDefinition.ContainerDefinitions[].Image` at that
ECR URI. Cold start drops to seconds, no runtime install, fully reproducible.

## Cleanup

```bash
polyris-deploy --destroy     # removes the pipeline stack
# Delete the eight AWS resources you provisioned in Step 1 if unused.
```

## Related

- API reference: [`docs/features/DATA_PASSING.md`](../../docs/features/DATA_PASSING.md)
- Focused xcom showcase: [`examples/16_xcom_showcase/`](../16_xcom_showcase/)
- All task types without xcom emphasis: [`examples/04_task_types/`](../04_task_types/)
