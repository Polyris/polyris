"""Scenario verbs over the real stack (ADR #130, Testing).

Scenarios speak only this API. Under it run the real templates (through the
interpreter), the real Lambdas in-process and moto tables built from the
template, so a scenario's text stays the same while the code under it
changes. A verb whose code path does not exist yet raises NeedsBinding; a
scenario that uses it carries `needs_binding(<stage>)` (conftest.STAGES)
until it exists.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Union

from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from bindings import Bindings
from boundary import Boundary, acting_as, as_component
from platform_stack import Stack
from row_invariants import CHECKS, STAGE_OF
import rows_view
from rows_view import Observer, StackView
from sfn_runtime import ACCOUNT, REGION, Execution, client_error
from streams import StreamAdapter

TASK_ARN = f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:user-{{}}"
_deserializer = TypeDeserializer()


class NeedsBinding(Exception):
    """The verb's code path is not wired yet; the scenario must be marked needs_binding(stage)."""

    def __init__(self, stage: str, what: str):
        super().__init__(f"{what} (arrives with the {stage} stage)")
        self.stage = stage


def plain(item: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _deserializer.deserialize(v) for k, v in item.items()}


class Clock:
    """Time for the stack. Every reading moves it 1 ms on, as real time would between two calls."""

    def __init__(self, start: datetime):
        self.now = start

    def read(self) -> datetime:
        self.now += timedelta(milliseconds=1)
        return self.now

    def text(self) -> str:
        now = self.read()
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)

    def jump_to(self, when: str) -> None:
        """Move the clock on to `when` (ISO 8601, UTC); never back."""
        target = datetime.fromisoformat(when.replace("Z", "+00:00"))
        if target < self.now:
            raise AssertionError(f"the clock is at {self.now.isoformat()}; it cannot go back to {when}")
        self.now = target


@dataclass
class Pipeline:
    dag: Any
    arn: str

    @property
    def name(self) -> str:
        return str(self.dag.dag_id)


@dataclass
class Run:
    pipeline: Pipeline
    name: str
    date: str
    arn: str

    @property
    def short(self) -> str:
        return self.name[-20:].replace(".", "").replace(":", "")

    def execution_name(self, task: str) -> str:
        return f"{task}-{self.date}-{self.short}"


class World:
    clock: Clock
    stack: Stack
    runtime: Any
    ddb: Any
    resource: Any
    bindings: Bindings
    boundary: Boundary

    def __init__(self, ddb_client: Any, *, start: Optional[datetime] = None):
        import boto3

        self.clock = Clock(start or datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc))
        self.stack = Stack(ddb_client, now=self.clock.text)
        self.runtime = self.stack.runtime
        self.ddb = ddb_client
        self.resource = boto3.resource("dynamodb", region_name="us-east-1")
        #: Every DynamoDB call a component makes, and the races scenarios order (boundary.py).
        self.boundary = Boundary([self.ddb, self.resource.meta.client])
        self._run_ids = itertools.count(0xABCD0000)
        self.bindings = Bindings(self.stack, self.resource, self.clock.read)
        self.bindings.start_run()
        self._dispatch = self.bindings.dispatcher(self.bindings.run_task_stub())
        self.bindings.query_subscriptions()
        self._fanout = as_component("lambda:asset-fanout", self.bindings.asset_fanout())
        self._console = self.bindings.console()
        self._sweeper = as_component("lambda:sweeper", self.bindings.sweeper())
        self._producers: Dict[str, Pipeline] = {}
        self._stream = StreamAdapter.for_mapping(self.stack, "AssetEventsFanoutMapping", table="AssetEventsTable",
                                                 key="asset_name")
        #: The tasks table's stream, which wakes the dispatcher (ADR #137 §7); scenarios deliver it.
        self._tasks_stream = StreamAdapter.for_mapping(self.stack, "TasksDispatchMapping",
                                                       table="PipelineTokensTable", key="execution_name")
        #: The rows a pass may decide before its budget runs out (shrink_pass_budget); None: no limit.
        self._pass_rows: Optional[int] = None
        #: What ADR #137's invariants read that no table keeps (rows_view.py), and what they found.
        import dispatcher

        self._reader = boto3.client("dynamodb", region_name="us-east-1")    # no hook sees its reads
        self._observer = Observer(self._read_row, self._run_items, self.boundary.log)
        self._observer.install(self.runtime, self.stack.machine_arn("RunTaskHelperSfn"), dispatcher._DISPATCHER)
        self.boundary.watch(self.stack.tables["PipelineTokensTable"], self._read_row)
        self.violations: List[str] = []
        #: Violations a test makes on purpose, to see a check find them (``plant``).
        self.planted: List[str] = []

    def plant(self, violation: str) -> None:
        """A violation this test made on purpose, to see a check find it: the end of the scenario does not
        count it."""
        self.planted.append(violation)

    def close(self) -> None:
        self.bindings.close()

    # ── setup ──────────────────────────────────────────────────────────

    def deploy(self, dag: Any, *, before_upgrade: bool = False) -> Pipeline:
        """Deploy as polyris-deploy does: load the machine, then run it register_only, so the
        real registration states write the registry entry and any push subscriptions.

        `before_upgrade`: a pipeline generated before 1.3, whose run is in flight across the upgrade.
        """
        if before_upgrade:
            raise NeedsBinding("rows-migration", "a pipeline generated before 1.3")
        pipeline = Pipeline(dag, self.stack.deploy_pipeline(dag))
        # A fresh name per deploy, as polyris-deploy does, so a pipeline can be deployed again.
        self.runtime.start_execution(stateMachineArn=pipeline.arn, name=f"{pipeline.name}-register-{next(self._run_ids):08x}",
                                     input=json.dumps({"register_only": True}))
        self.settle()
        return pipeline

    def start_run(self, pipeline: Pipeline, date: str, **inputs: Any) -> Run:
        name = f"{pipeline.name}-run-{date}-{next(self._run_ids):08x}"
        payload = {"current_date": date, **inputs}
        arn = self.runtime.start_execution(stateMachineArn=pipeline.arn, name=name,
                                           input=json.dumps(payload))["executionArn"]
        self.settle()
        return Run(pipeline, name, date, arn)

    def fire_schedule(self, pipeline: Pipeline, scheduled_time: str) -> Run:
        """Start `pipeline` as EventBridge Scheduler does: with the input polyris-deploy gives its
        schedule, `<aws.scheduler.scheduled-time>` filled in with `scheduled_time` (ISO 8601, UTC)."""
        from polyris.deploy import _generate_cfn_template

        template = _generate_cfn_template(
            pipeline.dag, namespace="acme", stage="dev", region="us-east-1",
            role_arn="arn:aws:iam::123456789012:role/acme-dev-polyris-orchestration-role",
            start_run_function_arn=self.stack.function_arn("StartRunFunction"),
            registry_table=self.stack.tables["PipelineRegistryTable"],
            tokens_table=self.stack.tables["PipelineTokensTable"],
            asset_subscriptions_table=self.stack.tables["AssetSubscriptionsTable"],
        )
        target = template["Resources"]["PipelineSchedule"]["Properties"]["Target"]
        payload = str(target["Input"]).replace("<aws.scheduler.scheduled-time>", scheduled_time)
        name = f"{pipeline.name}-sched-{next(self._run_ids):08x}"
        arn = self.runtime.start_execution(stateMachineArn=pipeline.arn, name=name, input=payload)["executionArn"]
        self.settle()
        return Run(pipeline, name, scheduled_time[:10], arn)

    def start_bare(self, pipeline: Pipeline) -> Run:
        """Start `pipeline` with an empty input, as `aws stepfunctions start-execution` without --input."""
        name = f"{pipeline.name}-bare-{next(self._run_ids):08x}"
        started = self.runtime.start_execution(stateMachineArn=pipeline.arn, name=name, input="{}")
        self.settle()
        return Run(pipeline, name, str(started["startDate"])[:10], started["executionArn"])

    def settle(self) -> None:
        """Run every execution until it waits, then check ADR #137's invariants of the bound stages
        (row_invariants.py, stages.py). ADR #130's invariants retired with its engine (D115)."""
        self.runtime.run()
        for violation in self.check_invariants():
            if violation not in self.violations:
                self.violations.append(violation)

    def check_invariants(self) -> List[str]:
        from stages import BOUND_STAGES

        view = self.rows_view()
        return [v for n, check in sorted(CHECKS.items()) if STAGE_OF.get(n) in BOUND_STAGES for v in check(view)]

    def rows_view(self) -> StackView:
        """The stack as ADR #137's invariants read it, now."""
        return StackView(self.clock.now, self._items(), list(self.runtime.executions.values()),
                         self.stack.machine_arn("RunTaskHelperSfn"), self._observer)

    def _read_row(self, key: str) -> Optional[Dict[str, Any]]:
        item = self._reader.get_item(TableName=self.stack.tables["PipelineTokensTable"],
                                     Key={"execution_name": {"S": key}}, ConsistentRead=True).get("Item")
        return rows_view.plain(item)

    def _items(self) -> List[Dict[str, Any]]:
        kwargs: Dict[str, Any] = {"TableName": self.stack.tables["PipelineTokensTable"], "ConsistentRead": True}
        items: List[Dict[str, Any]] = []
        while True:
            page = self._reader.scan(**kwargs)
            items += [rows_view.plain(i) or {} for i in page.get("Items", [])]
            if "LastEvaluatedKey" not in page:
                return items
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def _run_items(self, run: str) -> List[Dict[str, Any]]:
        return [i for i in self._items() if i.get("pipeline_execution") == run and "generation" in i
                and not str(i["execution_name"]).startswith("_")]

    # ── tasks ──────────────────────────────────────────────────────────

    def task_runs(self, run: Run, task: str) -> int:
        """How many run_task executions `task` of `run` has started, superseded ones included."""
        name = run.execution_name(task)
        helper = self.stack.machine_arn("RunTaskHelperSfn")
        return sum(1 for e in self.runtime.executions.values()
                   if e.machine.arn == helper and isinstance(e.input, dict) and e.input.get("execution_name") == name)

    def running_tasks(self, run: Run) -> List[str]:
        """Tasks of `run` whose user work has started and not finished."""
        names = {run.execution_name(t.task_id): t.task_id for t in run.pipeline.dag.tasks}
        return sorted(names[n] for n in (self._job_owner(j) for j in self.runtime.jobs.values()) if n in names)

    def complete(self, run: Run, task: str, *, status: str = "success", output: Any = None) -> None:
        """Finish the user's work for `task`; `status` is "success" or "failed", and a success returns
        `output` (an empty object when None)."""
        name = run.execution_name(task)
        job = next((j for j in self.runtime.jobs.values() if self._job_owner(j) == name), None)
        if job is None:
            raise AssertionError(f"{task} is not running in {run.name}; running: {self.running_tasks(run)}")
        if status == "success":
            self.runtime.finish_job(job.id, output={"Output": {} if output is None else output, "Status": "SUCCEEDED"})
        elif status == "failed":
            self.runtime.finish_job(job.id, error="States.TaskFailed", cause="the user's task failed")
        else:
            raise ValueError(f"status must be 'success' or 'failed', got {status!r}")
        self.settle()

    @staticmethod
    def _job_owner(job: Any) -> Optional[str]:
        payload = job.frame.execution.input
        return payload.get("execution_name") if isinstance(payload, dict) else None

    # ── failures injected from outside ─────────────────────────────────

    def stop_wrapper_externally(self, run: Run, task: str) -> None:
        """Someone stops the task's wrapper execution in the AWS console."""
        self.runtime.stop_execution(executionArn=self.wrapper(run, task).arn, error="Console", cause="stopped by hand")
        self.settle()

    def time_out_registration(self, run: Run, task: str) -> None:
        """The wrapper's wait for dependencies reaches its timeout."""
        wrapper = self.wrapper(run, task)
        token = next((t for t, f in self.runtime.tokens.items()
                      if f.execution is wrapper and f.state == "Wait_For_Dependencies"), None)
        if token is None:
            raise AssertionError(f"{task} is not waiting for its dependencies")
        self.runtime.time_out(token)
        self.settle()

    def time_out_pause_wait(self, run: Run, task: str) -> None:
        """The task's pause wait in run_task reaches its heartbeat timeout (12 hours by default)."""
        name = run.execution_name(task)
        helper = self.stack.machine_arn("RunTaskHelperSfn")
        token = next((t for t, f in self.runtime.tokens.items()
                      if f.execution.machine.arn == helper and f.state == "Save_Pause_Token_And_Wait"
                      and isinstance(f.execution.input, dict) and f.execution.input.get("execution_name") == name),
                     None)
        if token is None:
            raise AssertionError(f"{task} is not held in its pause wait")
        self.runtime.time_out(token)
        self.settle()

    # ── verbs whose code paths arrive with later phases ────────────────

    def write_event(self, asset: Any, partition: str, *, writer: str = "run_task") -> Optional[Run]:
        """`writer` records a fact of `asset` for `partition`; its hint waits for deliver_hints.

        The run_task writer is the real one: a producer pipeline whose task has
        `outlets=[asset]` runs for the partition and succeeds. The "older
        version" writer stores the row run_task wrote before ADR #130 (no
        partition_date, no granularity), as an upgraded stack still holds them.
        """
        if writer == "older version":
            self.resource.Table(self.stack.tables["AssetEventsTable"]).put_item(Item={
                "asset_name": asset.name, "event_time": self.clock.text(), "uri": "", "source_task": "build",
                "source_dag": "legacy_producer", "execution_date": partition,
                "ttl": int(self.clock.read().timestamp()) + 365 * 86400,
            })
            return None
        if writer != "run_task":
            raise NeedsBinding("operations", f"the {writer} writer")
        from dags import pipeline

        producer = self._producers.get(asset.name)
        if producer is None:
            safe = "".join(c if c.isalnum() else "_" for c in asset.name)
            producer = self._producers[asset.name] = self.deploy(pipeline(f"producer_{safe}", [("build", {"outlets": [asset]})]))
        run = self.start_run(producer, partition)
        self.complete(run, "build")
        return run

    def deliver_hints(self, asset: Any = None) -> None:
        """Deliver the stream records not yet delivered — all of them, or `asset`'s only."""
        name = None if asset is None else asset.name
        self._stream.deliver(self._fanout, select=None if name is None
                             else lambda r: r["dynamodb"]["NewImage"]["asset_name"]["S"] == name)
        self.settle()

    def _call_console(self, path: str, *, query: Dict[str, str], body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """POST to the console API through its real handler, as the UI does."""
        event = {
            "rawPath": path, "headers": {}, "queryStringParameters": query, "body": json.dumps(body or {}),
            "requestContext": {"http": {"method": "POST"}, "requestId": "harness", "stage": "$default"},
        }
        with acting_as("console"):
            response = self._console.handler(event, None)
        if response["statusCode"] >= 300:
            raise AssertionError(f"POST {path} → {response['statusCode']}: {response.get('body')}")
        self.settle()
        return json.loads(response["body"])

    def _task_action(self, path: str, run: Run, task: str) -> Dict[str, Any]:
        return self._call_console(path, query={"name": task}, body={"date": run.date, "pipeline_execution": run.name})

    def pause(self, run: Run) -> None:
        self._call_console("/api/execution-pause", query={"id": run.name})

    def resume(self, run: Run) -> Dict[str, Any]:
        return self._call_console("/api/execution-resume", query={"id": run.name})

    def restart(self, run: Run, task: str) -> None:
        self._task_action("/api/task-restart", run, task)

    def stop_run(self, run: Run) -> Dict[str, Any]:
        """The console's Stop of a whole run."""
        return self._call_console("/api/execution-stop", query={"pipeline_execution": run.name})

    def expire_ttl(self, table: str = "PipelineTokensTable") -> List[str]:
        """DynamoDB's TTL deletes every row of `table` (a logical id) whose `ttl` has passed on the clock.

        The deletions are the service's: a stream adapter on that table marks their REMOVE records with
        TTL's `userIdentity`. Returns the keys of the rows deleted.
        """
        name = self.stack.tables[table]
        now = self.clock.read().timestamp()
        schema = self.ddb.describe_table(TableName=name)["Table"]["KeySchema"]
        expired, kwargs = [], {"TableName": name, "ConsistentRead": True}
        while True:
            page = self.ddb.scan(**kwargs)
            expired += [i for i in page.get("Items", []) if "ttl" in i and float(i["ttl"]["N"]) <= now]
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        hash_key = next(k["AttributeName"] for k in schema if k["KeyType"] == "HASH")
        keys = [next(iter(item[hash_key].values())) for item in expired]
        if self._stream.table == name:
            self._stream.mark_expired(set(keys))
        for item in expired:
            self.ddb.delete_item(TableName=name, Key={k["AttributeName"]: item[k["AttributeName"]] for k in schema})
        return keys

    def delete_row(self, run: Run, task: str) -> None:
        """Someone deletes `task`'s row of `run` by hand (invariant 11's missing row)."""
        self.ddb.delete_item(TableName=self.stack.tables["PipelineTokensTable"],
                             Key={"execution_name": {"S": run.execution_name(task)}})

    def delete_output(self, run: Run, task: str) -> None:
        """`task`'s output for `run`'s date is gone, as when it expired a year after its last write (D109)."""
        self.ddb.delete_item(TableName=self.stack.tables["PipelineTokensTable"],
                             Key={"execution_name": {"S": f"output#{run.pipeline.name}#{task}#{run.date}"}})

    # ── tasks as rows (ADR #137): wired by the phases of 1.3 ──────────

    def delete_run_row(self, run: Run) -> None:
        """Someone deletes `run`'s run row by hand."""
        from polyris.coordination import run_row

        self.ddb.delete_item(TableName=self.stack.tables["PipelineTokensTable"],
                             Key={"execution_name": {"S": run_row.key(run.name)}})

    def hold_start_run_retry(self, pipeline: Pipeline) -> None:
        """Start_Run's next attempt writes its run as creating and dies, as a Lambda that times out after its
        first write; Step Functions retries it, and the retry waits for `release_start_run_retry`."""

        def die() -> None:
            failure = RuntimeError("Sandbox.Timedout (injected): the attempt died after writing its run row")
            failure.states_error = "Sandbox.Timedout"  # type: ignore[attr-defined]
            raise failure

        # Start_Run's first put follows its run row's write: the run's input.
        self.boundary.between(then=die, before_write=lambda call: call.component == "lambda:start-run"
                              and call.operation == "PutItem")
        self.runtime.hold_retries(pipeline.arn, "Start_Run")

    def release_start_run_retry(self, pipeline: Pipeline) -> None:
        """The retry `hold_start_run_retry` held goes on, late."""
        self.runtime.release_retries(pipeline.arn, "Start_Run")
        self.settle()

    def shrink_pass_budget(self, *, rows: int) -> None:
        """A pass's budget runs out after `rows` rows, as a large run's does at the declared maximum.

        The dispatcher checks its budget before each row: its clock ticks one second per reading, and
        each invocation gets `rows` seconds above the reserve it keeps."""
        import dispatcher

        ticks = itertools.count()
        self.bindings._set(dispatcher._DISPATCHER, "monotonic", lambda: float(next(ticks)))
        self._pass_rows = rows

    def run_to_the_end(self) -> None:
        """Move the clock past every deadline and sweep until nothing changes; then check liveness
        (row_invariants.liveness): every run ended or is paused, or a row waits for a decision."""
        raise NeedsBinding("rows-core", "the liveness check over the rows")

    def restart_with_dependants(self, run: Run, task: str) -> None:
        """The console's Restart of `task` and of its dependants that failed or were skipped by a rule."""
        raise NeedsBinding("rows-ops", "Restart with dependants")

    def upgrade_platform(self) -> None:
        """Install 1.3 over the 1.2 platform the World runs, with the runs in flight left as they are."""
        raise NeedsBinding("rows-migration", "the upgrade from 1.2")

    # ── the tasks table's stream (ADR #137 §7): wired with the dispatcher in TR-4 ──

    def deliver_task_hints(self, run: Optional["Run"] = None) -> None:
        """Deliver the tasks table's stream to the dispatcher, every record or `run`'s, until none is left:
        a pass's own writes, such as `left_at`, wake the passes after it."""
        select = None if run is None else (lambda record: _run_of(record) == run.name)
        for _ in range(10_000):
            self.runtime.run()
            if not self._tasks_stream.deliver(self._dispatch_in_budget, select=select):
                return
        raise AssertionError("the tasks table's stream never ran dry")

    def _dispatch_in_budget(self, event: Any, _: Any) -> Any:
        from dispatcher import RESERVE_MS

        rows = self._pass_rows
        remaining = (lambda: RESERVE_MS + rows * 1000) if rows is not None else (lambda: 10 ** 9)
        return self._dispatch(event, _Invocation(remaining))

    def lose_task_hint(self, run: "Run", task: str) -> None:
        """The next record of `task`'s row in `run` that the mapping would deliver is lost."""
        name = run.execution_name(task)
        self._tasks_stream.drop(lambda record: _key_of(record) == name, times=1)

    def delay_task_hint(self, run: "Run", task: str) -> None:
        """The next record of `task`'s row in `run` that the mapping would deliver waits for `release_task_hints`."""
        name = run.execution_name(task)
        self._tasks_stream.delay(lambda record: _key_of(record) == name, times=1)

    def release_task_hints(self) -> None:
        """The delayed stream records arrive, after the ones delivered meanwhile."""
        self._tasks_stream.release()

    def two_passes_at_once(self, run: "Run") -> None:
        """Two passes over `run` at once, as when two shards of the stream carry records of one run:
        the second runs between the first's read and its first write."""
        import dispatcher
        from polyris.coordination import run_row

        def second() -> None:
            dispatcher._DISPATCHER.full_pass(run_row.key(run.name), source="second", remaining_ms=lambda: 10 ** 9)

        tokens = self.stack.tables["PipelineTokensTable"]
        # The read of the run's task rows: a pass's first read of its spec version comes earlier.
        self.boundary.between(then=second, after_read=lambda call: call.component == "lambda:reconcile"
                              and call.operation == "BatchGetItem" and call.tables == [tokens])

    def sweep(self) -> None:
        """One scheduled run of the sweeper, at the clock's current time."""
        self._sweeper({}, None)
        self.settle()

    def mark_success(self, run: Run, task: str) -> Dict[str, Any]:
        return self._task_action("/api/task-success", run, task)

    def skip(self, run: Run, task: str) -> None:
        self._task_action("/api/task-skip", run, task)

    def fail_task(self, run: Run, task: str) -> None:
        """The console's Fail of one task."""
        self._task_action("/api/task-fail", run, task)

    def stop_task(self, run: Run, task: str) -> None:
        """The console's Stop of one task: a waiting task ends `aborted`, a running one `stopped`."""
        self._task_action("/api/task-stop", run, task)

    def push_runs(self, pipeline: Pipeline) -> List[Run]:
        """Runs of `pipeline` that a fact started (AssetFanout's push), oldest first."""
        runs = []
        for execution in self.runtime.executions.values():
            payload = execution.input if isinstance(execution.input, dict) else {}
            if execution.machine.arn == pipeline.arn and payload.get("triggered_by") == "asset":
                runs.append(Run(pipeline, execution.name, payload["variables"]["current_date"], execution.arn))
        return runs

    # ── faults ─────────────────────────────────────────────────────────

    def inject_failure(self, operation: str, table: str, code: str, *,
                       when: Optional[Callable[[Dict[str, Any]], bool]] = None, times: Optional[int] = None,
                       after: bool = False) -> None:
        """Make DynamoDB `operation` on `table` (a logical id) fail with `code`.

        `when` narrows it to calls whose API parameters match; `times` limits
        how many calls fail (default: every matching call). With `after`, the
        call is applied and only its answer fails, as a write whose response is
        lost to a 5xx or a timeout.
        """
        name = self.stack.tables[table]
        left = [times]
        marker = f"polyris-fail-after-{id(left)}"

        def fail(params: Dict[str, Any], context: Optional[Dict[str, Any]] = None, **_: Any) -> None:
            if params.get("TableName") != name or (when is not None and not when(params)):
                return
            if left[0] is not None:
                if left[0] <= 0:
                    return
                left[0] -= 1
            if after and context is not None:
                context[marker] = True
                return
            raise ClientError({"Error": {"Code": code, "Message": f"{code} (injected)"}}, operation)

        def fail_answer(context: Optional[Dict[str, Any]] = None, **_: Any) -> None:
            if context is not None and context.pop(marker, False):
                raise ClientError({"Error": {"Code": code, "Message": f"{code} (injected after the write)"}}, operation)

        # Both clients: the interpreter's (state-machine integrations) and the Lambdas'.
        for client in (self.ddb, self.resource.meta.client):
            client.meta.events.register(f"before-parameter-build.dynamodb.{operation}", fail)
            if after:
                client.meta.events.register(f"after-call.dynamodb.{operation}", fail_answer)

    def fail_starts(self, target: Union[Pipeline, str], code: str, *, times: int) -> None:
        """The next `times` starts of `target` fail with `code` (e.g. throttling): a Lambda's
        StartExecution and a state machine's `states:startExecution` integration alike.

        `target` is a deployed pipeline, or a platform state machine's logical id.
        """
        arn = target.arn if isinstance(target, Pipeline) else self.stack.machine_arn(target)
        runtime, real, left = self.runtime, self.runtime._start, [times]

        def _start(machine_arn: str, name: Optional[str], payload: Any) -> Any:
            if machine_arn == arn and left[0] > 0:
                left[0] -= 1
                raise client_error(code, "StartExecution", f"{code} (injected)")
            return real(machine_arn, name, payload)

        runtime._start = _start  # type: ignore[method-assign]

    def fail_signals(self, code: str, *, times: int, call: str = "send_task_success",
                     when: Optional[Callable[[Dict[str, Any]], bool]] = None) -> None:
        """The next `times` SendTaskSuccess calls (or `call="send_task_failure"`), from any caller,
        fail with `code` (e.g. throttling); `when` narrows it to calls whose arguments match."""
        runtime, real, left = self.runtime, getattr(self.runtime, call), [times]
        operation = "".join(part.title() for part in call.split("_"))

        def send(**kwargs: Any) -> Dict[str, Any]:
            if left[0] > 0 and (when is None or when(kwargs)):
                left[0] -= 1
                raise client_error(code, operation, f"{code} (injected)")
            return real(**kwargs)

        setattr(runtime, call, send)

    def fail_stops(self, code: str, *, times: int) -> None:
        """The next `times` StopExecution calls fail with `code`, as a throttled stop would."""
        runtime, real, left = self.runtime, self.runtime.stop_execution, [times]

        def stop_execution(**kwargs: Any) -> Dict[str, Any]:
            if left[0] > 0:
                left[0] -= 1
                raise client_error(code, "StopExecution", f"{code} (injected)")
            return real(**kwargs)

        runtime.stop_execution = stop_execution

    def fail_invokes(self, function: str, *, times: int, error: str = "CoordinationTransient") -> None:
        """The next `times` invocations of a platform function (logical id) by a state machine fail
        with `error`: CoordinationTransient, the error its Lambda reports for a transient failure,
        or what Step Functions reports for the Lambda itself, such as Sandbox.Timedout for a timeout."""
        from failures import CoordinationTransient

        name = self.stack.functions[function]
        real, left = self.runtime.lambdas[name], [times]

        def handler(event: Any, context: Any) -> Any:
            if left[0] > 0:
                left[0] -= 1
                if error == "CoordinationTransient":
                    raise CoordinationTransient("throttled (injected)")
                failure = RuntimeError(f"{error} (injected)")
                failure.states_error = error  # type: ignore[attr-defined]
                raise failure
            return real(event, context)

        self.runtime.lambdas[name] = handler

    def hold(self, machine: str) -> None:
        """New executions of a platform state machine (logical id) wait to take a step until `release`."""
        self.runtime.hold(self.stack.machine_arn(machine))

    def release(self, machine: str) -> None:
        self.runtime.release(self.stack.machine_arn(machine))
        self.settle()

    def release_newest(self, machine: str) -> None:
        """Let only the newest held execution go on and settle, to order a race; the others stay held."""
        self.runtime.release_newest(self.stack.machine_arn(machine))
        self.settle()

    # ── observations ───────────────────────────────────────────────────

    def signals(self, run: Run, task: str) -> List[Dict[str, Any]]:
        """Callbacks the task's wrapper accepted while waiting for its dependencies, oldest first."""
        arn = self.wrapper(run, task).arn
        return [c["outcome"] for c in self.runtime.callbacks
                if c["execution"] == arn and c["state"] == "Wait_For_Dependencies"]

    def warnings(self) -> List[Dict[str, Any]]:
        """Coordination warnings as the bell reads them: through the attention index."""
        from polyris.coordination import attention as att

        found: List[Dict[str, Any]] = []
        for partition in att.partitions(att.WARN):
            response = self.ddb.query(TableName=self.stack.tables["PipelineTokensTable"], IndexName=att.INDEX,
                                      KeyConditionExpression="#a = :p", ExpressionAttributeNames={"#a": att.KEY},
                                      ExpressionAttributeValues={":p": {"S": partition}})
            found.extend(plain(i) for i in response.get("Items", []))
        return found


    def push_subscribers(self, asset: Any) -> List[str]:
        """Pipelines subscribed to `asset` for a push (the asset-subscriptions table), by name."""
        response = self.ddb.query(TableName=self.stack.tables["AssetSubscriptionsTable"], ConsistentRead=True,
                                  KeyConditionExpression="asset_name = :a",
                                  ExpressionAttributeValues={":a": {"S": asset.name}})
        return sorted(str(plain(i)["pipeline_name"]) for i in response.get("Items", []))

    def row(self, run: Run, task: str) -> Optional[Dict[str, Any]]:
        item = self.ddb.get_item(TableName=self.stack.tables["PipelineTokensTable"],
                                 Key={"execution_name": {"S": run.execution_name(task)}}, ConsistentRead=True)
        return plain(item["Item"]) if "Item" in item else None

    def status(self, run: Run, task: str) -> Optional[str]:
        row = self.row(run, task)
        return None if row is None else str(row.get("status"))

    def pipeline_status(self, run: Run) -> str:
        return self.runtime.executions[run.arn].status

    def wrapper(self, run: Run, task: str) -> Execution:
        suffix = run.name[-8:] if len(run.name) > 8 else run.name
        name = f"{task}-{run.date}-{suffix}"[:80]
        wrappers = self.stack.machine_names["DependencyWrapperSfn"]   # the current one, or the v1.1.4 one
        found = [e for e in self.runtime.executions.values()
                 if e.machine.name.startswith(wrappers) and e.name == name]
        if not found:
            raise AssertionError(f"no wrapper execution {name}")
        return found[0]


@dataclass
class _Invocation:
    """The Lambda context the tasks stream's mapping hands the dispatcher: its time left."""
    get_remaining_time_in_millis: Callable[[], int]


def _key_of(record: Dict[str, Any]) -> str:
    return str(record["dynamodb"]["Keys"]["execution_name"]["S"])


def _run_of(record: Dict[str, Any]) -> Optional[str]:
    """The run a tasks-table record belongs to: the run row's own, or a task row's `pipeline_execution`."""
    from polyris.coordination import run_row

    key = _key_of(record)
    if run_row.is_run_row(key):
        return key[len(run_row.KEY_PREFIX):]
    image = record["dynamodb"].get("NewImage") or record["dynamodb"].get("OldImage") or {}
    value = image.get("pipeline_execution", {}).get("S")
    return str(value) if value else None
