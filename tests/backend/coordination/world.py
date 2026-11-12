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
from typing import Any, Callable, Dict, List, Optional

from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from bindings import Bindings
from platform_stack import Stack
from sfn_runtime import ACCOUNT, REGION, Execution

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

    def text(self) -> str:
        self.now += timedelta(milliseconds=1)
        return self.now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{self.now.microsecond // 1000:03d}Z"

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


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
        self.bindings = Bindings(self.stack, self.resource, lambda: self.clock.now)
        self.bindings.reconcile()
        self.bindings.query_subscriptions()
        self._fanout = self.bindings.asset_fanout()
        self._producers: Dict[str, Pipeline] = {}
        self._stream = StreamAdapter(self.stack)

    def close(self) -> None:
        self.bindings.close()

    # ── setup ──────────────────────────────────────────────────────────

    def deploy(self, dag: Any) -> Pipeline:
        """Deploy as polyris-deploy does: load the machine, then run it register_only, so the
        real registration states write the registry entry and any push subscriptions."""
        pipeline = Pipeline(dag, self.stack.deploy_pipeline(dag))
        self.runtime.start_execution(stateMachineArn=pipeline.arn, name=f"{pipeline.name}-register",
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

    def settle(self) -> None:
        self.runtime.run()

    # ── tasks ──────────────────────────────────────────────────────────

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

    # ── verbs whose code paths arrive with later phases ────────────────

    def write_event(self, asset: Any, partition: str, *, writer: str = "run_task") -> Run:
        """`writer` records a fact of `asset` for `partition`; its hint waits for deliver_hints.

        The run_task writer is the real one: a producer pipeline whose task has
        `outlets=[asset]` runs for the partition and succeeds.
        """
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

    def pause(self, run: Run) -> None:
        raise NeedsBinding("operations", "pause through the console route")

    def resume(self, run: Run) -> None:
        raise NeedsBinding("operations", "resume → reconcile_run")

    def restart(self, run: Run, task: str) -> None:
        raise NeedsBinding("operations", "restart replays starter_input")

    def sweep(self) -> None:
        raise NeedsBinding("sweeper", "the sweeper")

    def mark_success(self, run: Run, task: str) -> None:
        raise NeedsBinding("assets", "the console's mark_success writes the fact for the run's partition")

    def skip(self, run: Run, task: str) -> None:
        raise NeedsBinding("operations", "the console's skip while the wrapper waits")

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
                       when: Optional[Callable[[Dict[str, Any]], bool]] = None, times: Optional[int] = None) -> None:
        """Make DynamoDB `operation` on `table` (a logical id) fail with `code`.

        `when` narrows it to calls whose API parameters match; `times` limits
        how many calls fail (default: every matching call).
        """
        name = self.stack.tables[table]
        left = [times]

        def fail(params: Dict[str, Any], **_: Any) -> None:
            if params.get("TableName") != name or (when is not None and not when(params)):
                return
            if left[0] is not None:
                if left[0] <= 0:
                    return
                left[0] -= 1
            raise ClientError({"Error": {"Code": code, "Message": f"{code} (injected)"}}, operation)

        # Both clients: the interpreter's (state-machine integrations) and the Lambdas'.
        for client in (self.ddb, self.resource.meta.client):
            client.meta.events.register(f"before-parameter-build.dynamodb.{operation}", fail)

    # ── observations ───────────────────────────────────────────────────

    def signals(self, run: Run, task: str) -> List[Dict[str, Any]]:
        """Callbacks the task's wrapper accepted while waiting for its dependencies, oldest first."""
        arn = self.wrapper(run, task).arn
        return [c["outcome"] for c in self.runtime.callbacks
                if c["execution"] == arn and c["state"] == "Wait_For_Dependencies"]

    def warnings(self) -> List[Dict[str, Any]]:
        """Coordination warnings as the bell reads them: through the attention index."""
        from coordination_core import attention as att

        found: List[Dict[str, Any]] = []
        for partition in att.partitions(att.WARN):
            response = self.ddb.query(TableName=self.stack.tables["PipelineTokensTable"], IndexName=att.INDEX,
                                      KeyConditionExpression="#a = :p", ExpressionAttributeNames={"#a": att.KEY},
                                      ExpressionAttributeValues={":p": {"S": partition}})
            found.extend(plain(i) for i in response.get("Items", []))
        return found


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
        found = [e for e in self.runtime.executions.values()
                 if e.machine.arn == self.stack.machine_arn("DependencyWrapperSfn") and e.name == name]
        if not found:
            raise AssertionError(f"no wrapper execution {name}")
        return found[0]
