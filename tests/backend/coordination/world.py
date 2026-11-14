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
from invariants import Invariants
from platform_stack import Stack
from sfn_runtime import ACCOUNT, REGION, Execution, client_error

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


class StreamAdapter:
    """The asset-events stream as the event source mapping delivers it (template settings applied).

    Records are read from moto's stream, filtered with the mapping's
    FilterCriteria, and handed to the handler in batches of its BatchSize.
    Records the handler reports as failed stay pending and are delivered again.
    """

    def __init__(self, stack: Stack):
        import boto3

        props = stack.resources["AssetEventsFanoutMapping"]["Properties"]
        self.batch_size = int(props["BatchSize"])
        self.patterns = [json.loads(f["Pattern"]) for f in props["FilterCriteria"]["Filters"]]
        table = stack.tables["AssetEventsTable"]
        self.arn = stack.ddb.describe_table(TableName=table)["Table"]["LatestStreamArn"]
        self.streams = boto3.client("dynamodbstreams", region_name="us-east-1")
        self.seen: set = set()
        self.pending: List[Dict[str, Any]] = []

    def _matches(self, record: Dict[str, Any]) -> bool:
        return any(all(record.get(key) in values for key, values in pattern.items()) for pattern in self.patterns)

    def _read(self) -> None:
        for shard in self.streams.describe_stream(StreamArn=self.arn)["StreamDescription"]["Shards"]:
            iterator = self.streams.get_shard_iterator(StreamArn=self.arn, ShardId=shard["ShardId"],
                                                       ShardIteratorType="TRIM_HORIZON")["ShardIterator"]
            for record in self.streams.get_records(ShardIterator=iterator)["Records"]:
                sequence = record["dynamodb"]["SequenceNumber"]
                if sequence not in self.seen:
                    self.seen.add(sequence)
                    if self._matches(record):
                        self.pending.append({**record, "eventSourceARN": self.arn})

    def deliver(self, handler: Callable[[Any, Any], Any], *, asset: Optional[str] = None) -> None:
        self._read()
        chosen = [r for r in self.pending
                  if asset is None or r["dynamodb"]["NewImage"]["asset_name"]["S"] == asset]
        held = [r for r in self.pending if r not in chosen]
        failed: List[Dict[str, Any]] = []
        for start in range(0, len(chosen), self.batch_size):
            batch = chosen[start:start + self.batch_size]
            result = handler({"Records": batch}, None)
            ids = {f["itemIdentifier"] for f in result.get("batchItemFailures", [])}
            failed += [r for r in batch if r["dynamodb"]["SequenceNumber"] in ids]
        self.pending = held + failed


class World:
    clock: Clock
    stack: Stack
    runtime: Any
    ddb: Any
    resource: Any
    bindings: Bindings

    def __init__(self, ddb_client: Any, *, start: Optional[datetime] = None):
        import boto3

        self.clock = Clock(start or datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc))
        self.stack = Stack(ddb_client, now=self.clock.text)
        self.runtime = self.stack.runtime
        self.ddb = ddb_client
        self.resource = boto3.resource("dynamodb", region_name="us-east-1")
        self._run_ids = itertools.count(0xABCD0000)
        self.bindings = Bindings(self.stack, self.resource, self.clock.read)
        self.bindings.reconcile()
        self.bindings.query_subscriptions()
        self._fanout = self.bindings.asset_fanout()
        self._console = self.bindings.console()
        self._sweeper = self.bindings.sweeper()
        self._producers: Dict[str, Pipeline] = {}
        self._stream = StreamAdapter(self.stack)
        #: ADR #130 invariants 1, 3, 4, 5 and 10, checked each time the stack settles (invariants.py).
        self.invariants = Invariants(ddb_client, self.runtime, self.stack.tables)

    def close(self) -> None:
        self.bindings.close()

    # ── setup ──────────────────────────────────────────────────────────

    def deploy(self, dag: Any, *, before_upgrade: bool = False) -> Pipeline:
        """Deploy as polyris-deploy does: load the machine, then run it register_only, so the
        real registration states write the registry entry and any push subscriptions.

        `before_upgrade`: the pipeline's tasks start the wrapper as v1.1.4 defined it, as runs
        in flight across the upgrade do; every helper they call is the current one.
        """
        wrapper_arn = self.stack.legacy_wrapper_arn() if before_upgrade else None
        pipeline = Pipeline(dag, self.stack.deploy_pipeline(dag, wrapper_arn=wrapper_arn))
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
            wrapper_arn=self.stack.machine_arn("DependencyWrapperSfn"),
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
        self.runtime.run()
        self.invariants.observe()

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

    def complete(self, run: Run, task: str, *, status: str = "success") -> None:
        """Finish the user's work for `task`; `status` is "success" or "failed"."""
        name = run.execution_name(task)
        job = next((j for j in self.runtime.jobs.values() if self._job_owner(j) == name), None)
        if job is None:
            raise AssertionError(f"{task} is not running in {run.name}; running: {self.running_tasks(run)}")
        if status == "success":
            self.runtime.finish_job(job.id, output={"Output": {}, "Status": "SUCCEEDED"})
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
        self._stream.deliver(self._fanout, asset=None if asset is None else asset.name)
        self.settle()

    def _call_console(self, path: str, *, query: Dict[str, str], body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """POST to the console API through its real handler, as the UI does."""
        event = {
            "rawPath": path, "headers": {}, "queryStringParameters": query, "body": json.dumps(body or {}),
            "requestContext": {"http": {"method": "POST"}, "requestId": "harness", "stage": "$default"},
        }
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
