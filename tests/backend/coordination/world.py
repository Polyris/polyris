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
from event_bus import EventBus
from platform_stack import Stack
from row_invariants import CHECKS, STAGE_OF, liveness
import rows_view
from rows_view import Observer, StackView
from sfn_runtime import ACCOUNT, REGION, client_error
from streams import StreamAdapter

from polyris.coordination.times import format_utc

TASK_ARN = f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:user-{{}}"
#: A work that returns JSON null (`World.complete`'s None means an empty object).
JSON_NULL = object()
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
    #: The tasks table's stream delivers its records whenever the stack settles, as the mapping does in AWS.
    #: Off by default: a scenario that orders passes delivers them by hand (``deliver_task_hints``).
    tasks_stream_runs: bool = False

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
        #: Every call run_task made to the notify function: the alerts of a failure, the resolve after a success.
        self.notifications: List[Dict[str, Any]] = []
        self._dispatch = self.bindings.dispatcher(self.bindings.run_task(self.notifications))
        self._fanout = as_component("lambda:asset-fanout", self.bindings.asset_fanout())
        self._console = self.bindings.console()
        self._sweeper = as_component("lambda:sweeper", self.bindings.sweeper())
        self._producers: Dict[str, Pipeline] = {}
        self._stream = StreamAdapter.for_mapping(self.stack, "AssetEventsFanoutMapping", table="AssetEventsTable",
                                                 key="asset_name")
        #: The tasks table's stream, which wakes the dispatcher (ADR #137 §7); scenarios deliver it.
        self._tasks_stream = StreamAdapter.for_mapping(self.stack, "TasksDispatchMapping",
                                                       table="PipelineTokensTable", key="execution_name")
        #: The end of an execution, which the template's rule sends to the dispatcher (ADR #137 §7). Its events
        #: are delivered whenever the stack settles, as EventBridge delivers them within seconds.
        self._execution_ends = EventBus.for_rule(self.stack, "ExecutionEndRule")
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
        #: What ends each failure a scenario injected (``run_to_the_end`` heals the world before liveness).
        self._healers: List[Callable[[], None]] = []

    def plant(self, violation: str) -> None:
        """A violation this test made on purpose, to see a check find it: the end of the scenario does not
        count it."""
        self.planted.append(violation)

    def close(self) -> None:
        self.bindings.close()

    # ── setup ──────────────────────────────────────────────────────────

    def deploy(self, dag: Any) -> Pipeline:
        """Deploy as polyris-deploy does: load the machine, then run it register_only, so the
        real registration states write the registry entry and any push subscriptions.
        """
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
            schedule_role_export="acme-dev-polyris-schedule-role-arn",
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
        (row_invariants.py, stages.py) and that no template expression failed (D107). ADR #130's
        invariants retired with its engine (D115)."""
        self.runtime.run()
        self._deliver_execution_ends()
        if self.tasks_stream_runs:
            self.deliver_task_hints()
        for violation in self.check_invariants():
            if violation not in self.violations:
                self.violations.append(violation)

    def check_invariants(self) -> List[str]:
        """ADR #137's invariants of the bound stages, and every template expression that failed (D107)."""
        from stages import BOUND_STAGES

        view = self.rows_view()
        return ([v for n, check in sorted(CHECKS.items()) if STAGE_OF.get(n) in BOUND_STAGES for v in check(view)]
                + self._expression_failures())

    def _expression_failures(self) -> List[str]:
        """A template expression that failed, caught or not (D107): always a template bug, and a Catch can hide
        it. The interpreter fails a state the way AWS does, an expression that evaluates to undefined included:
        `Failure_Complete` read fields its input no longer had, and every scenario passed while the live failure
        handler failed (1.2.0, CR-7). No template is excepted: those that knew failing ones went in 1.3 (TR-8)."""
        return [f"expression failed in {failure['machine']}.{failure['state']}: {failure['cause']}"
                for failure in self.runtime.expression_errors]

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

    def complete(self, run: Run, task: str, *, status: str = "success", output: Any = None,
                 cause: str = "the user's task failed") -> None:
        """Finish the user's work for `task`; `status` is "success", "failed" or "timed_out" (the attempt reached
        its execution_timeout), a success returns `output` (an empty object when None, null for JSON_NULL), and a
        failure ends with `cause`."""
        name = run.execution_name(task)
        job = next((j for j in self.runtime.jobs.values() if self._job_owner(j) == name), None)
        if job is None:
            raise AssertionError(f"{task} is not running in {run.name}; running: {self.running_tasks(run)}")
        if status == "success":
            returned = {} if output is None else None if output is JSON_NULL else output
            self.runtime.finish_job(job.id, output={"Output": returned, "Status": "SUCCEEDED"})
        elif status == "failed":
            self.runtime.finish_job(job.id, error="States.TaskFailed", cause=cause)
        elif status == "timed_out":
            self.runtime.finish_job(job.id, error="States.Timeout", cause="the attempt reached its execution_timeout")
        else:
            raise ValueError(f"status must be 'success', 'failed' or 'timed_out', got {status!r}")
        self.settle()

    def stores(self, run: Run, task: str) -> Dict[str, Optional[Dict[str, Any]]]:
        """The rows of `task`'s date that run_task writes beside its row: `output#` and `input#`."""
        pipeline = run.pipeline.name
        return {kind: self._read_row(f"{kind}#{pipeline}#{task}#{run.date}") for kind in ("output", "input")}

    def timeline(self, run: Run, task: str) -> List[Dict[str, Any]]:
        """`task`'s timeline events (task-events), oldest first, as the console reads them."""
        response = self.ddb.query(TableName=self.stack.tables["TaskEventsTable"], IndexName="execution-name-index",
                                  KeyConditionExpression="execution_name = :e",
                                  ExpressionAttributeValues={":e": {"S": run.execution_name(task)}})
        return sorted((plain(i) for i in response.get("Items", [])), key=lambda e: str(e["event_time"]))

    def run_task_transitions(self, run: Run, task: str) -> List[int]:
        """The state transitions of each run_task execution of `task`, oldest first."""
        name, helper = run.execution_name(task), self.stack.machine_arn("RunTaskHelperSfn")
        return [e.transitions for e in self.runtime.executions.values()
                if e.machine.arn == helper and isinstance(e.input, dict) and e.input.get("execution_name") == name]

    def work_input(self, run: Run, task: str) -> Dict[str, Any]:
        """What the user's work of `task` was given: the input of its state machine (dags.py builds `@task.sfn`)."""
        name = run.execution_name(task)
        job = next((j for j in self.runtime.jobs.values() if self._job_owner(j) == name), None)
        if job is None:
            raise AssertionError(f"{task} is not running in {run.name}; running: {self.running_tasks(run)}")
        payload = job.arguments.get("Input", {})
        return json.loads(payload) if isinstance(payload, str) else dict(payload)

    def xcom_get(self, run: Run, task: str, upstream: str, **flags: Any) -> Any:
        """What `task`'s code gets from `xcom.get(event, upstream, **flags)`, `event` being what its work was given."""
        from polyris import xcom

        return xcom.get(self.work_input(run, task), upstream, ddb_client=self.ddb, **flags)

    @staticmethod
    def _job_owner(job: Any) -> Optional[str]:
        payload = job.frame.execution.input
        return payload.get("execution_name") if isinstance(payload, dict) else None

    # ── failures injected from outside ─────────────────────────────────

    def stop_run_task_externally(self, run: Run, task: str) -> None:
        """Someone stops the task's run_task execution in the AWS console."""
        name, helper = run.execution_name(task), self.stack.machine_arn("RunTaskHelperSfn")
        found = [e for e in self.runtime.executions.values() if e.machine.arn == helper and e.status == "RUNNING"
                 and isinstance(e.input, dict) and e.input.get("execution_name") == name]
        if not found:
            raise AssertionError(f"{task} has no running run_task in {run.name}")
        self.runtime.stop_execution(executionArn=found[0].arn, error="Console", cause="stopped by hand")
        self.settle()

    def stop_run_externally(self, run: Run, *, error: str = "Console") -> None:
        """Someone stops the run's own execution in the AWS console, with `error` or none (""); its end wakes the
        engine, which stops the run, or its next check does when the event is lost."""
        self.runtime.stop_execution(executionArn=run.arn, error=error, cause="stopped by hand" if error else "")
        self.settle()

    # ── the end of an execution on EventBridge (ADR #137 §7) ───────────

    def _deliver_execution_ends(self) -> None:
        for _ in range(10_000):
            if not self._execution_ends.deliver():
                return
            self.runtime.run()
        raise AssertionError("the executions' ends never ran dry")

    def _run_task_of(self, run: Run, task: str) -> Callable[[Dict[str, Any]], bool]:
        name, helper = run.execution_name(task), self.stack.machine_arn("RunTaskHelperSfn")
        execution = self.runtime.executions

        def of_task(event: Dict[str, Any]) -> bool:
            found = execution.get(event["detail"]["executionArn"])
            return (found is not None and found.machine.arn == helper and isinstance(found.input, dict)
                    and found.input.get("execution_name") == name)
        return of_task

    def lose_execution_end(self, run: Run, task: Optional[str] = None) -> None:
        """The next end of `run`'s execution, or of `task`'s run_task, never reaches the dispatcher: EventBridge
        delivers on a best-effort basis."""
        if task is None:
            self._execution_ends.drop(lambda event: event["detail"]["executionArn"] == run.arn)
        else:
            self._execution_ends.drop(self._run_task_of(run, task))

    def execution_ends(self, run: Run, task: Optional[str] = None) -> List[Dict[str, Any]]:
        """The ends of `run`'s execution, or of `task`'s run_task executions, that reached the dispatcher."""
        of = (lambda event: event["detail"]["executionArn"] == run.arn) if task is None else self._run_task_of(run, task)
        return [event for event in self._execution_ends.delivered if of(event)]

    def executions_ended(self, run: Run, task: Optional[str] = None) -> List[Dict[str, Any]]:
        """Every end of `run`'s execution, or of `task`'s run_task executions, that Step Functions sent to the
        bus, whether the rule let it through or not."""
        of = (lambda event: event["detail"]["executionArn"] == run.arn) if task is None else self._run_task_of(run, task)
        return [event for event in self.runtime.events if of(event)]

    def send_execution_end(self, event: Dict[str, Any]) -> Any:
        """An end of an execution on the bus, from the test: one that comes again, or of another platform. Returns
        what the dispatcher answered, or None when the rule did not let it through."""
        if not self._execution_ends.publish(event):
            return None
        result = self._execution_ends.results[-1]
        self.settle()
        return result

    def describes(self) -> List[Any]:
        """Every DescribeExecution call the dispatcher made, with the source of the pass that made it."""
        return list(self._observer.describes)

    def facts(self, asset: Any) -> List[Dict[str, Any]]:
        """The facts recorded for `asset`, oldest first."""
        response = self.ddb.query(TableName=self.stack.tables["AssetEventsTable"],
                                  KeyConditionExpression="asset_name = :a",
                                  ExpressionAttributeValues={":a": {"S": asset.name}}, ConsistentRead=True)
        return [plain(i) for i in response.get("Items", [])]

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

    def write_output(self, pipeline: Pipeline, task: str, date: str, text: str) -> None:
        """`task`'s output of `date` as an older version left it: `text` is the stored result, as it was written."""
        self.ddb.put_item(TableName=self.stack.tables["PipelineTokensTable"],
                          Item={"execution_name": {"S": f"output#{pipeline.name}#{task}#{date}"}, "result": {"S": text}})

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

    #: At most this many rounds of ``run_to_the_end``: a round that changes nothing ends it sooner.
    END_ROUNDS = 30

    def run_to_the_end(self) -> List[str]:
        """Heal the world, move the clock past every deadline and sweep until nothing changes; then check
        liveness (row_invariants.liveness): every run ended or is paused, or a row waits for a decision.

        Each round finishes the user's work still running, delivers both streams, moves the clock to the
        earliest entry the sweeper takes and sweeps. A round that changes nothing moves the clock past the
        latest due time of a row, so that every deadline passes, and sweeps once more; when that changes
        nothing either, nothing more happens on its own. A liveness violation fails the scenario at its
        end, as an invariant's does (ADR #137, Tests); the violations are also returned."""
        self._heal()
        for _ in range(self.END_ROUNDS):
            before = self._state()
            self._finish_jobs()
            self.deliver_hints()
            self.deliver_task_hints()
            due = self._next_due()
            if due is None:
                break
            self._sweep_at(due)
            if self._state() != before:
                continue
            last = self._next_due(latest=True, rows_only=True)
            if last is None or last <= self.clock.now:
                break
            self._sweep_at(last)
            if self._state() == before:
                break
        found = liveness(self.rows_view())
        for violation in found:
            if violation not in self.violations:
                self.violations.append(violation)
        return found

    def _sweep_at(self, when: datetime) -> None:
        if when > self.clock.now:
            self.clock.jump_to(format_utc(when))
        self.sweep()

    def _state(self) -> Any:
        """What ``run_to_the_end`` watches for a change: each row's status and generation, the marks of the
        run rows, the warnings, and each execution's status."""
        rows = sorted((str(i["execution_name"]), str(i.get("status")), str(i.get("generation")),
                       str(i.get("stop")), str(i.get("stop_done")), str(i.get("answered_order")))
                      for i in self._items())
        executions = sorted((e.arn, e.status) for e in self.runtime.executions.values())
        return rows, executions

    def _heal(self) -> None:
        """Every failure a scenario injected ends, and whatever it held goes on: executions, Start_Run's
        retries, stream records, arranged interleavings and a shrunk pass budget. A lost record stays lost."""
        for heal in self._healers:
            heal()
        self._healers.clear()
        self.boundary.heal()
        self._stream.heal()
        self._tasks_stream.heal()
        self._execution_ends.heal()
        self._pass_rows = None
        for machine in list(self.runtime.held):
            self.runtime.release(machine)
        for machine, state in list(self.runtime.held_retries):
            self.runtime.release_retries(machine, state)
        self.settle()

    def _finish_jobs(self) -> None:
        """The user's work still running ends well: the jobs of run_task's stand-in, and the jobs their ends let
        start, until none is left, so a chain of any length ends within one round."""
        run_task = self.stack.machine_arn("RunTaskHelperSfn")
        for _ in range(100_000):
            jobs = [job for job in self.runtime.jobs.values() if job.frame.execution.machine.arn == run_task]
            if not jobs:
                return
            for job in jobs:
                self.runtime.finish_job(job.id, output={"Output": {}, "Status": "SUCCEEDED"})
            self.settle()
            self.deliver_task_hints()
        raise AssertionError("the user's work never ran out")

    def _next_due(self, *, latest: bool = False, rows_only: bool = False) -> Optional[datetime]:
        """The earliest (or ``latest``) due time of the entries the sweeper takes: the waiting and decision
        kinds and the push retries, and the run rows' unless ``rows_only``."""
        from polyris.coordination import attention as att
        from polyris.coordination import run_row
        from polyris.coordination.times import parse_utc

        kinds = (att.WAITING, att.DECISION, att.RETRY)
        dues = [parse_utc(str(item[att.DUE])) for item in self._items()
                if str(item.get(att.KEY, "")).split("#", 1)[0] in kinds and item.get(att.DUE)
                and not (rows_only and run_row.is_run_row(str(item["execution_name"])))]
        return (max if latest else min)(dues, default=None)

    def restart_with_dependants(self, run: Run, task: str) -> None:
        """The console's Restart of `task` and of its dependants that failed or were skipped by a rule."""
        self._call_console("/api/task-restart", query={"name": task},
                           body={"date": run.date, "pipeline_execution": run.name, "with_dependants": True})

    def upgrade_platform(self, *, statuses: Optional[Dict[str, str]] = None) -> None:
        """Make the runs the World started into what a 1.2 platform left in flight when it was upgraded (D6).

        The World runs 1.3's engine alone, so the runs it started stand in for 1.2's and are made into what 1.2
        leaves: their executions were stopped as the update's cleanup began; they have no run row and no
        run input; their task rows carry none of 1.3's fields (readiness, spec version, run start); and each
        waiting row keeps the `waiting` entry 1.2 gave it, first due after 10 minutes. `statuses` gives a task's
        row a status that only 1.2 wrote, by task name, such as `waiting_delay` for a countdown of wait_before.
        """
        from polyris.coordination import attention, run_row, task_row
        from polyris.coordination.times import format_utc

        table = self.resource.Table(self.stack.tables["PipelineTokensTable"])
        rows = [i for i in self._items() if task_row.is_task_key(str(i["execution_name"])) and "generation" in i]
        for execution in list(self.runtime.executions.values()):
            if execution.status == "RUNNING":
                self.runtime.stop_execution(executionArn=execution.arn, cause="polyris-up: the platform runs 1.3")
        for run in sorted({str(i["pipeline_execution"]) for i in rows}):
            table.delete_item(Key={run_row.TABLE_KEY: run_row.key(run)})
            table.delete_item(Key={run_row.TABLE_KEY: run_row.input_key(run)})
        due = format_utc(self.clock.read() + attention.BACKOFF[0])
        for item in rows:
            names = {"#r": task_row.READINESS, "#h": task_row.SPEC_HASH, "#v": task_row.SPEC_ID,
                     "#s": task_row.RUN_START}
            expression, values = "REMOVE #r, #h, #v, #s", {}
            older = (statuses or {}).get(str(item.get("task_name")))
            if older is not None:
                names["#st"] = task_row.STATUS
                values = {":st": older}
                expression = "SET #st = :st " + expression
            elif item.get("status") == "waiting":
                names.update({"#att": attention.KEY, "#due": attention.DUE})
                values = {":att": attention.attention(attention.WAITING, str(item["execution_name"])), ":due": due}
                expression = "SET #att = :att, #due = :due " + expression
            table.update_item(Key={"execution_name": item["execution_name"]}, UpdateExpression=expression,
                              ExpressionAttributeNames=names, **({"ExpressionAttributeValues": values} if values else {}))

    # ── the tasks table's stream (ADR #137 §7): wired with the dispatcher in TR-4 ──

    def deliver_task_hints(self, run: Optional["Run"] = None) -> None:
        """Deliver the tasks table's stream to the dispatcher, every record or `run`'s, until none is left:
        a pass's own writes, such as `left_at`, wake the passes after it."""
        select = None if run is None else (lambda record: _run_of(record) == run.name)
        for _ in range(10_000):
            self.runtime.run()
            self._deliver_execution_ends()
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
            # A transaction names its tables in its items: it fails as a whole when one of them is `table`.
            tables = {params.get("TableName")} | {body.get("TableName") for item in params.get("TransactItems", [])
                                                  for body in item.values()}
            if name not in tables or (when is not None and not when(params)):
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
        self._healers.append(lambda: left.__setitem__(0, 0))

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
        self._healers.append(lambda: left.__setitem__(0, 0))

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
        self._healers.append(lambda: left.__setitem__(0, 0))

    def fail_stops(self, code: str, *, times: int) -> None:
        """The next `times` StopExecution calls fail with `code`, as a throttled stop would."""
        runtime, real, left = self.runtime, self.runtime.stop_execution, [times]

        def stop_execution(**kwargs: Any) -> Dict[str, Any]:
            if left[0] > 0:
                left[0] -= 1
                raise client_error(code, "StopExecution", f"{code} (injected)")
            return real(**kwargs)

        runtime.stop_execution = stop_execution
        self._healers.append(lambda: left.__setitem__(0, 0))

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
        self._healers.append(lambda: left.__setitem__(0, 0))

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
