# Glossary

The words the polyris docs and console use, for anyone who writes or runs pipelines. The engine's own terms, for contributors, are in [ARCHITECTURE.md § Glossary](docs/architecture/ARCHITECTURE.md#glossary).

## Pipeline

A DAG of tasks in a `dag.py` file, named by its `dag_id`. `polyris-deploy` deploys it as its own [pipeline stack](#pipeline-stack). See [DSL.md](docs/features/DSL.md).

## Task

One unit of work in a pipeline: a decorated function such as `@task.lambda_function(...)` that starts an AWS resource you already have. In each run, a task has its own status, retries and output.

## Run

One execution of a pipeline for one [date](#a-runs-date). A schedule, **Run** in the console, or an asset's [fact](#fact) starts it. It ends once every task is final, and fails if a task failed.

## A run's date

The date a run is for, the same for all its tasks: the date you pick when you press **Run**, the UTC day the schedule fired, or the date of the fact that started it. It can also name an hour, a week or a month (`2026-01-12T06`, `2026-W03`, `2026-01`). Asset docs call it the run's *partition*. See [DSL.md § Date variables](docs/features/DSL.md#date-variables).

## Schedule preset

A shorthand for a common schedule: `@hourly`, `@daily`, `@weekly`, `@monthly` or `@yearly`, each a cron at the start of its period in UTC. See [DSL.md § Schedule options](docs/features/DSL.md#schedule-options).

## Asset

A named piece of data, such as a table or a set of files, that tasks produce (`outlets`) and that other pipelines start on (`schedule=`) or wait for (`wait_for`). See [ASSETS.md](docs/features/ASSETS.md).

## Fact

The record that an asset has data for a date. A producer task's success records one per asset in its `outlets`, dated by its run's date; **Mark Successful** records them too.

## wait_for

A task's list of assets it waits for: it starts once each has a fact for its run's date, and fails after 7 days without one. See [ASSETS.md § Pull-based dependencies](docs/features/ASSETS.md#pull-based-dependencies-wait_for).

## Trigger rule

The condition on a task's direct upstream tasks that decides when it runs; `all_success` by default. See [DSL.md § Trigger rules](docs/features/DSL.md#trigger-rules).

## Decision

A task that failed after its retries waits for you, shown as **Needs decision** (`waiting_decision`): restart it, mark it successful, skip it or fail it in the console. Without a decision, it fails after 5 hours. See [Notifications](docs/features/notifications.md#what-to-do-with-one).

## Direct step

A single AWS service call, such as an SNS message or an S3 write, that runs in the pipeline's own state machine without a task. It starts with the run and has no dependencies. See [DSL.md § Direct steps](docs/features/DSL.md#direct-steps).

## Namespace

The prefix you give `polyris-up`, such as your company's name. It starts the name of every resource polyris creates, so installs in one account don't collide.

## Stage

The name of one install next to the namespace, such as `dev` or `prod` (`dev` by default). One account can hold a platform per stage.

## Platform

What `polyris-up` installs once per namespace and stage: the console, its API, and the tables and functions that run every pipeline's tasks. Its CloudFormation stack is named `<namespace>-<stage>`.

## Pipeline stack

The CloudFormation stack `polyris-deploy` creates for one pipeline: its state machine, its schedule and its log group. It is named `<namespace>-<stage>-polyris-<dag_id>`, and `polyris-destroy` deletes it.

## Spec version

What `polyris-deploy` records of a pipeline's tasks before it deploys the stack. A run keeps the spec version it started with, so a redeploy changes the tasks from the next run on.
