# Roadmap

Where Polyris is heading. This is a high-level, user-facing view of planned
capabilities — not a commitment to dates or ordering. For shipped features see
[CHANGELOG.md](../../CHANGELOG.md).

Have a request or want to help build one of these? Open an issue or see
[CONTRIBUTING.md](../../CONTRIBUTING.md).

## Planned

### Pipeline authoring
- **`@task.python`** — package a Python function as a Lambda and deploy it
  directly via `polyris-deploy`, no separate packaging step.
- **DAG diff on deploy** — show what changed in a pipeline's structure before
  applying.
- **Dependency-aware backfill** — backfills that respect upstream/downstream
  asset relationships.

### Observability
- **Cost tracking per pipeline** — attribute AWS spend to individual pipelines.
- **Execution diff** — compare two runs side by side.
- **SLA indicators** — surface lateness and SLA status in the console.
- **Browser notifications** — alert on failure without watching the dashboard.
- **Task logs viewer** — read task logs directly in the UI.

### Operations at scale
- **Multi-region support** — run pipelines across more than one AWS region.
- **Role-based access control** — finer-grained permissions for teams.
- **Audit logging** — a record of who changed what, when.

### Quality of life
- **Asset lineage graph** — visualize asset dependencies as an interactive graph in the console (CLI: `polyris-output --graph` is already available).

### Distribution & onboarding
Goal: shrink "clone the repo + run SAM by hand" down to installing a package and
clicking deploy. The infrastructure itself is CloudFormation, so AWS provisioning
time (~10 min, CloudFront-bound) can't be removed — but the manual steps around it can.
- **Publish the CLI to PyPI** — `pip install polyris` for the DSL + `polyris-*`
  commands, so authoring/validating pipelines needs no repo clone. `pyproject.toml`
  already declares the entry points; this is mostly release wiring. *(Cheapest, independent.)*
- **One-click "Deploy to AWS"** — `sam package` the stack into a self-contained
  template (18 local `CodeUri` → `s3://` artifacts) published to the public releases
  bucket (the one `sam/bootstrap.yaml` creates), then a CloudFormation launch URL /
  `templateURL=...`. Deploys the infra with no clone; CI publishes it each release
  via the existing OIDC role.
- **UI bundled in the stack** — a CloudFormation custom resource (extend the existing
  `ui_bootstrap` Lambda) that, on stack create, pulls the pre-built UI bundle from the
  public releases bucket and writes it + `config.js` (real API/Cognito values) into the
  user's `ConsoleUiBucket`. Removes the separate `deploy.sh` step. *(Hardest piece —
  this is what makes it feel "packaged.")*
- **AWS Serverless Application Repository (SAR)** — `sam publish` so Polyris is
  discoverable and deployable straight from the AWS console. *(Polish on top of the
  launch URL.)*
- **Surface the local "try without AWS" path** — lead onboarding with
  `pip install polyris` → `polyris-init demo --local` → `polyris-output --graph` so
  newcomers can explore the DSL/DAG in ~60s before committing to an AWS deploy.
- **Drop the `deploy.sh` stack-name footgun** — resolve `stack_name`/`region` from
  `samconfig.toml` (single source of truth) instead of a hidden `polyris-dev` default,
  so `./deploy.sh` needs no manually-synced args. *(Small; has already bitten once.)*

## Under consideration

Ideas being explored but not yet committed. Feedback welcome:

- Per-DAG execution budget (a monthly spend cap per pipeline).
- SLA escalation chains (tiered alerting).
- Cost estimator in the backfill UI (preview spend before running).

## How priorities are set

Polyris is built around a small, sharp core: a Python DSL that compiles to AWS
Step Functions, deployed serverlessly at low cost. Features that keep that core
simple and composable are favored over ones that add operational weight. If a
capability can live as a thin layer on top of the DSL rather than complicating
the engine, that is the preferred shape.

## Data passing — remaining wiring (after `xcom.pull()` runtime, part 1)

`polyris.xcom.pull()` is **wired end-to-end and usable** — items 1–2 below are done
(context injection into every task type + the read IAM policy). The remaining items
are optional/deferred:

1. ~~**Inject context into each task type's native channel**~~ — **DONE + verified.**
   run_task now injects pipeline name + run date (`date`, the store's key field) +
   table into every task: `pipeline_name`/`date`/`_polyris_table` in the **Lambda
   payload** and **SFN input**; `POLYRIS_*` **env** appended to **ECS** and **Batch**
   ContainerOverrides (PascalCase, per AWS docs); `--POLYRIS_*` **job arguments** for
   **Glue**. Verified by JSONata evaluation (semantic tests in `tests/sfn_jsonata`)
   and the full suite. `pull()` reads all sources (explicit > context > env), keyed
   on `date` to match the store.
2. ~~**Grant IAM** DynamoDB read (and S3 read for offloaded outputs) to task roles~~
   — **DONE.** Polyris references user-owned compute by name/ARN and cannot attach to
   those roles, so it publishes a managed policy `PolyrisTaskReadPolicy` (least
   privilege: `dynamodb:GetItem` + `s3:GetObject`) that users attach to any task role
   calling `pull()`. See docs/features/DATA_PASSING.md.
3. **Offload large outputs to S3 on write** — **DEFERRED** (do only if a real >350 KB inline need appears). Rationale: the inline limit is now 350 KB, which covers realistic outputs; genuinely large data is passed as an `s3://` pointer anyway; and a size-check Choice state would add a per-task transition on the STANDARD run_task machine — a real (if tiny) cost on *every* task for a rare feature. `pull()` already resolves `{_ref}` S3 pointers, so only the write side (Choice + `s3:putObject` + rerouting the 7 success transitions) remains if we ever do it.
   exceeds the threshold, write it to `RESULTS_BUCKET` and store an
   `{_ref: "s3://..."}` pointer instead of the current `{_truncated}` marker.
   `pull()` already resolves such pointers.
4. **Keep the auto-`upstream` injection** (decision reversed — **WON'T retire**).
   `Read_Upstream_Outputs` map and the `upstream` merges into Lambda/SFN input from
   the run_task template, and update the ~3 data-upstream tests + data-passing docs.
   (The `upstream_failed` *status* used by trigger rules is unrelated and stays.)

