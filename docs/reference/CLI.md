# CLI Reference

Every polyris command and its options. install.sh installs them all ([README → Install](../../README.md#install)); each takes `--help`.

| Command | What it does |
|---------|-------------|
| [`polyris`](#polyris) | Lists the commands; `--version` prints the installed version |
| [`polyris-up`](#polyris-up) | Installs or upgrades the platform in your AWS account |
| [`polyris-down`](#polyris-down) | Removes the platform |
| [`polyris-init`](#polyris-init) | Creates a pipeline folder, or a project's `config.py` |
| [`polyris-validate`](#polyris-validate) | Checks a pipeline file for errors |
| [`polyris-local`](#polyris-local) | Previews or simulates a pipeline without AWS |
| [`polyris-output`](#polyris-output) | Prints the generated definition, a graph or the assets |
| [`polyris-deploy`](#polyris-deploy) | Deploys a pipeline |
| [`polyris-destroy`](#polyris-destroy) | Removes a pipeline |
| [`polyris-register`](#polyris-register) | Registers a deployed pipeline in the console by hand |
| [`polyris-build-lambda`](#polyris-build-lambda) | Copies the `xcom` helpers into a Lambda handler's folder |

The pipeline commands read `dag.py` in the current folder (`--file` for another); how they load it: [How a pipeline file is loaded](#how-a-pipeline-file-is-loaded).

---

## polyris

Prints the list of commands and exits.

```bash
polyris              # print the command index
polyris --version    # print the installed version, e.g. "polyris 1.3.0"
```

The installed version is also the platform version `polyris-up` installs, so quote it in a bug report.

---

## polyris-up

Installs or upgrades the platform in your AWS account from the prebuilt release of the installed polyris version. Runs from any folder and writes nothing there.

```bash
polyris-up                                   # new install (asks for a namespace, a stage and an admin email) or upgrade
polyris-up --namespace acme --stage dev --admin-email you@acme.com --yes
polyris-up --param EnablePitr=true           # change one template parameter, keep the rest
polyris-up --namespace acme --stage prod     # a second stage next to an existing one
```

| Option | Description |
|--------|-------------|
| `--namespace` | Resource prefix for a new install. Bucket names are global, so it must be unique to you |
| `--stage` | Stage of a new install (default: `dev`). Namespace + Stage ≤ 33 characters |
| `--admin-email` | First console admin; Cognito emails a temporary password |
| `--param KEY=VALUE` | Set a template parameter (repeatable). Unset parameters keep their current value. `Namespace`, `Stage`, `ArtifactsBucket`, `UiArtifactsPrefix` are refused |
| `--stack` | Platform stack name (default: the one stack tagged as a polyris platform, or `<namespace>-<stage>` when those flags are given). Naming a platform stack created with `sam deploy` switches it to `polyris-up` (asks to confirm); a stack that isn't a polyris platform is refused |
| `--profile`, `--region` | AWS profile / region (default: `AWS_REGION`, then the standard credential chain; no region anywhere is an error) |
| `--archive` | Deploy a local release archive (from `scripts/build_platform_release.py`) instead of downloading one |
| `--yes` | Don't ask for confirmation |
| `--accept-data-loss` | Allow a change that replaces or removes a DynamoDB table, S3 bucket, the Cognito user pool or the admin user. `--yes` never does |
| `--allow-downgrade` | Allow installing an older release than the stack runs (a rollback), never below 1.4. A pre-release such as `1.3.0rc1` counts as older than `1.3.0` |
| `--disable-rollback` | If the update fails, keep what it already changed (`UPDATE_FAILED`) instead of rolling back. Fix the cause and run the same command again; if the release has nothing more to change, retry the failed update from the CloudFormation console. A first install always rolls back. Use it when the release notes ask for it |

The release archive is cached in `~/.cache/polyris/<version>/`; artifacts go to the bucket `polyris-artifacts-<account>-<region>`. A change that would replace or remove a DynamoDB table, S3 bucket, the Cognito user pool or the admin user is listed and needs confirmation (or `--accept-data-loss`). At the end of an upgrade, `polyris-up` lists the pipelines that need a redeploy, with the command for each.

`polyris-up` updates a platform from 1.3 or newer. A platform on 1.2 upgrades to 1.3 first with the polyris-up of 1.3: see [upgrade-1.2-to-1.3.md](../how-to/upgrade-1.2-to-1.3.md). Before it uploads or changes anything, it checks the account and stops with a list to fix: [What the checks may refuse](../how-to/manage-the-platform.md#what-the-checks-may-refuse). At its end it prints the deploy policy that pipeline authors need.

---

## polyris-down

Deletes the platform stack. Lists the DynamoDB tables (always deleted) and buckets with their object counts, then asks you to type the stack name.

```bash
polyris-down                    # refuses while ResultsBucket holds task results
polyris-down --delete-data      # also delete task results
```

| Option | Description |
|--------|-------------|
| `--delete-data` | Empty and delete `ResultsBucket` too |
| `--stack`, `--profile`, `--region` | As for `polyris-up` |
| `--yes` | Don't ask for confirmation |

It refuses a stack that isn't a polyris platform and a stack that is mid-update. Unless you pass `--yes`, it also refuses while pipelines are still deployed on the platform: every pipeline in its registry whose state machine still exists, plus stacks named `<namespace>-<stage>-polyris-*`. Remove those first with `polyris-destroy`. Even with `--yes`, it refuses while a pipeline stack imports the platform's schedule role: destroy the scheduled pipelines first. Before it empties the buckets, and until the stack is gone, it stops the running executions of the platform's state machines and prints how many of each. The artifacts bucket is deleted when no other platform stack is left in the region.

---

## polyris-init

Creates a pipeline folder with a `dag.py`, or a project's `config.py`.

```bash
# A first pipeline that deploys and runs without edits (press Run in the console)
polyris-init hello-world --hello

# A pipeline to try without AWS: example ARNs that polyris-validate and polyris-local accept
polyris-init my-pipeline --local

# A pipeline to edit and deploy (creates my-pipeline/dag.py)
polyris-init my-pipeline

# A project: writes pipelines/config.py; then create pipelines inside pipelines/, so they find it
polyris-init --project
cd pipelines && polyris-init my-pipeline

# A schedule other than @daily
polyris-init my-pipeline --schedule "@hourly"
polyris-init my-pipeline --schedule "cron(0 9 ? * MON-FRI *)"

# Interactive wizard
polyris-init my-pipeline -i
```

| Option | Description |
|--------|-------------|
| `name` | Pipeline name (optional with `--project` or `-i`) |
| `--hello` | Scaffold a pipeline that runs the platform's built-in test state machine: it deploys and runs without edits. Finds the platform as `polyris-deploy` does; accepts `--profile`, `--region`, `--stack` |
| `--local` | Scaffold a pipeline with example ARNs (account `123456789012`), to try the commands without AWS. Point the ARNs at your own state machines before you deploy |
| `--cfn` | Scaffold a pipeline to deploy (default) |
| `--project` | Write `pipelines/config.py` (creates `pipelines/` if missing) |
| `--schedule` | The schedule: a preset such as `@hourly`, `cron(…)` or `rate(…)` (default: `@daily`). A bare 5-field cron is refused |
| `--dir` | Base folder (default: `.`) |
| `-i`, `--interactive` | Interactive wizard |

---

## polyris-validate

Checks a pipeline file for errors, without AWS. Run it in the pipeline's folder.

```bash
polyris-validate              # validate dag.py in this folder
polyris-validate -v           # verbose: schedule and task count
polyris-validate --all        # every pipeline folder in this folder, as polyris-deploy --all deploys them
polyris-validate --all -v     # all pipelines, verbose
polyris-validate --file my_pipeline.py
polyris-validate --test       # run each task's Python body locally
polyris-validate --json       # JSON output
```

| Option | Description |
|--------|-------------|
| `--all`, `-a` | Validate the pipeline files of each folder directly in the current folder, as `polyris-deploy --all` deploys them, and check them against each other (asset cycles, a `dag_id` made twice) |
| `-v`, `--verbose` | Show the schedule and the task count |
| `--file`, `-f` | Pipeline file (default: `dag.py`) |
| `--test` | Run each task's Python body locally and report the ones that raise |
| `--json` | Output results as JSON |

It also refuses a `dag_id` that ends like one of the platform's state machines (`bulk-backfill`, `test-quick`), and, when `config.py` sets the stage's `account_id`, a task whose `role=` names a role of that account outside the path `/polyris-tasks/`.

---

## polyris-local

Previews, validates and simulates a pipeline without AWS. Run it in the pipeline's folder. Details and the Python API: [LOCAL_TESTING.md](../tools/LOCAL_TESTING.md).

```bash
polyris-local                    # dry run: the DAG, the order of its tasks, the validation result
polyris-local --validate         # exit 0 if valid, 1 if not
polyris-local --mock             # run each task's Python function, with the trigger rules
polyris-local --dry-run --asl    # dry run, with the generated Step Functions JSON
polyris-local --file other.py
```

| Option | Description |
|--------|-------------|
| `--validate` | Validate and exit 0/1 |
| `--mock` | Simulate a run with mock results |
| `--dry-run` | Show the execution plan (default when no mode is given) |
| `--asl` | Include the generated Step Functions JSON in the dry run |
| `--file`, `-f` | Pipeline file (default: `dag.py`) |

---

## polyris-output

Prints what polyris generates from a pipeline file, without AWS.

```bash
polyris-output --json                       # the Step Functions definition and its task specs
polyris-output --mermaid                    # a Mermaid diagram
polyris-output --graph                      # an ASCII graph
polyris-output --assets                     # the assets this file declares
polyris-output --json --file my_pipeline.py
polyris-output --json --select my-dag-id    # one DAG of a file with several
```

| Option | Description |
|--------|-------------|
| `--json` | The Step Functions definition (`definition`) and the task specs `polyris-deploy` writes (`specs`: `spec_id`, `task_count`, `format_version` and each task's spec) |
| `--mermaid` | A Mermaid diagram |
| `--graph` | An ASCII graph |
| `--assets` | The assets this pipeline file declares, with their producers and consumers within the file, as JSON |
| `--file`, `-f` | Pipeline file (default: `dag.py`) |
| `--select` | One DAG by its ID, for a file with several |

---

## polyris-deploy

Deploys a pipeline to AWS through a CloudFormation change set, with boto3: no AWS CLI or SAM CLI. Run it in the pipeline's folder.

```bash
polyris-deploy                        # the stage of config.py's DEFAULT_STAGE, or the one platform in the region
polyris-deploy --stage prod --profile my-aws-profile
polyris-deploy --dry-run              # show what it would deploy; changes nothing
polyris-deploy --all                  # every pipeline folder in this folder
```

| Option | Description |
|--------|-------------|
| `--stage` | Stage (default: `config.py`'s `DEFAULT_STAGE`). The pipeline file reads it as `POLYRIS_STAGE` |
| `--stack` | The platform stack to read outputs from (default: `config.py`'s `ENVIRONMENTS[stage].stack_name`, or the one `polyris-up` platform in the region) |
| `--profile` | AWS profile (default: from `config.py`, or the standard credential chain) |
| `--region` | AWS region (default: `POLYRIS_REGION` / `AWS_REGION`, then `config.py`, then your profile's region) |
| `--file` | Pipeline file (default: `dag.py`). Not used with `--all`/`--only` |
| `--select` | One DAG by its ID, for a file with several; with `--all`/`--only`, the DAG to deploy from each folder |
| `--all` | Every folder directly in the current folder, but hidden ones (`.venv`) and caches. Not with `--only` |
| `--only DIR [DIR ...]` | Only the listed folders. Not with `--all` |
| `--dry-run` | Show the stack, its resources and the spec version it would write; changes nothing |
| `--destroy` | Same as `polyris-destroy` |
| `--log-level` | The pipeline's CloudWatch log level: `ALL`, `ERROR`, `FATAL`, `OFF` (default: `ERROR`) |
| `--log-retention` | Log retention in days (default: `30`) |

**What it deploys.** One stack per pipeline, `<namespace>-<stage>-polyris-<dag_id>`, with:
- the pipeline's state machine (`AWS::StepFunctions::StateMachine`) and its log group;
- for a `cron(…)`, `rate(…)` or preset schedule, an EventBridge Scheduler schedule, which starts the state machine with the platform's schedule role. A pipeline scheduled on assets, with `@once` or with `schedule=None`, has none.

The stack holds no IAM role.

**Before the stack.** `polyris-deploy` refuses a task whose `role=` names a role of the platform's account outside the path `/polyris-tasks/` ([give-a-pipeline-its-own-permissions.md](../how-to/give-a-pipeline-its-own-permissions.md)). It checks that the AWS resources your tasks name exist, and stops if one does not. It writes each task's spec to the platform's `pipeline-specs` table, reads the version back and checks it; a spec it cannot write or confirm stops the deploy before the stack changes. A pipeline beyond a limit of [DSL.md → Pipeline limits](../features/DSL.md#pipeline-limits) is refused before anything reaches AWS. After the stack deploys, it records the pipeline in the console's registry and prints `✅ <dag_id> deployed successfully!`.

**Which platform.** The stack: `--stack`, then `stack_name` in `config.py`, then the one stack `polyris-up` tagged in the region; several tagged stacks and no name is an error that asks for `--stack`. Without a `config.py`, the namespace and the stage come from that platform stack.

**Needs:** the platform, installed with `polyris-up` ([QUICKSTART.md](../getting-started/QUICKSTART.md)), and AWS credentials for its account and region with the deploy permissions of [IAM_PERMISSIONS.md](../deployment/IAM_PERMISSIONS.md#deploy--per-pipeline-polyris-deploy).

### Bulk mode (`--all` / `--only`)

Run it from the folder that holds your pipeline folders (for example `cd examples`).

```bash
polyris-deploy --all                              # every folder
polyris-deploy --only orders inventory            # only these folders
polyris-deploy --only orders inventory --select orders-daily   # one DAG per folder; a folder without it is skipped
```

1. In each folder, `dag.py` and every `.py` file directly in it whose code imports `DAG`, `dag` or `Pipeline` from polyris are loaded, except test files, in a Python process of the folder's own. A folder with several pipeline files (`orders.py`, `inventory.py`) has all of them deployed; a Glue script beside them is never run.
2. Every DAG found in every file is deployed. A file with no DAG is skipped; a shared `config.py` or `utils.py` that imports no DAG maker is listed as not a pipeline file and never run.
3. Before anything is deployed, every folder's `dag_id`s are collected: a `dag_id` that two files make, in one folder or two, stops the run and nothing is deployed, since each DAG is one stack. With `--select`, only that `dag_id` counts. A folder whose `dag_id`s could not be read is not deployed and counts as a failure.
4. With `--all`, a pipeline file in the current folder itself, or deeper than one folder, is not deployed: a warning names it. Move it into its own folder, or deploy it with `--file`.

One DAG failing (a validation error, an AWS error) does not stop the others. A summary at the end lists what succeeded and what failed, and the exit code is non-zero if anything failed, so you can use it as a CI gate.

---

## polyris-destroy

Removes a pipeline: its stack, its running runs and its entry in the console. Takes the same options as `polyris-deploy`, bulk mode included.

```bash
polyris-destroy                  # the pipeline of dag.py
polyris-destroy --stage prod
polyris-destroy --all            # the pipeline of every folder
polyris-destroy --dry-run        # show what it would remove
```

It stops the pipeline's running runs until the stack is gone; a task already running may finish first. To stop work at once, stop the runs in the console first.

---

## polyris-register

Registers a deployed pipeline in the console's registry, without running it. Use it when a deployed pipeline is missing from the console ([REGISTRATION.md](../features/REGISTRATION.md)).

```bash
polyris-register arn:aws:states:us-east-1:123456789012:stateMachine:mycompany-dev-polyris-my-pipeline
polyris-register --name my-pipeline
polyris-register --name my-pipeline --profile prod --region us-east-1
polyris-register --name my-pipeline --role-arn arn:aws:iam::123456789012:role/deploy
polyris-register --name my-pipeline --json
```

| Option | Description |
|--------|-------------|
| `arn` | The pipeline's state machine ARN (positional, optional) |
| `--name`, `-n` | Pipeline name (instead of the ARN) |
| `--region`, `-r` | AWS region (default: the region of the ARN, or of your AWS profile) |
| `--profile`, `-p` | AWS profile |
| `--namespace` | Namespace prefix for the search by name |
| `--role-arn` | IAM role ARN to assume (cross-account) |
| `--json` | Output the result as JSON |

---

## polyris-build-lambda

Copies the `xcom` helpers into a Lambda handler's folder, so the handler can `from polyris import xcom` without installing polyris in its package. Rebuild the package after you upgrade polyris.

```bash
polyris-build-lambda lambdas/load_orders          # writes lambdas/load_orders/polyris/
polyris-build-lambda lambdas/load_orders --force  # replace an existing polyris/ folder
```

| Option | Description |
|--------|-------------|
| `dir` | The Lambda handler's folder (positional) |
| `--force` | Overwrite an existing `polyris/` folder |

---

## How a pipeline file is loaded

`polyris-validate`, `polyris-deploy`, `polyris-output` and `polyris-local` load a pipeline file the same way, so a file that validates also deploys.

- A pipeline is every DAG the file's code makes: with or without `with`, through `@dag`, a helper function, or other files it runs. A DAG that a module it imports makes is not one.
- While the file runs, its folder and then the folder above it come first on `sys.path`, and `sys.exit(0)` ends it normally.
- Each DAG is one stack, so a `dag_id` made twice is refused.
- With `--all`, `polyris-validate` and `polyris-deploy` load the folders directly in the current folder, each in its own Python process, so one folder's modules never reach another's. In each folder they load `dag.py` and every `.py` whose code imports `DAG`, `dag` or `Pipeline` from polyris, except test files: a Glue script that imports only `xcom` is never run, whatever its comments say. Both warn about a pipeline file in the current folder itself or deeper, which `--all` does not load, and refuse a `dag_id` that two files make.
