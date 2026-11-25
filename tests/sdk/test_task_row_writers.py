"""Every writer of a task row or a run row, and how it may write (ADR #137 invariant 7; ADR #130 §2.1).

The writers of 1.3 answer to the transition table (``transitions.py``, ADR #137 §3).
A pass of the dispatcher moves a task row only as the table gives the move to the
dispatcher, on the status it read, and checks the run row's ``run_version`` with
each decision. ``Start_Run`` creates task rows only as the table gives creation to
it. Every writer of the run row raises ``run_version`` exactly when the table says
so. The coordination Lambdas write task rows and the run row only through the
builders of ``dispatch_writes`` and ``start_run``.

The writers of 1.2 that remain answer to ADR #130 §2.1 (D40) until they move onto
the table: a write made for an owner is fenced by the generation, and a write that
ends an ownership raises it.

Each census finds its writers in the code and in the state-machine templates, so a
new writer fails here until it is given a role.
"""

import ast
import functools
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, Mapping, Tuple

import pytest
from sfn_jsonata import load_template

from polyris import generators
from polyris.constants import TaskStatus
from polyris.coordination import dispatch, dispatch_writes, row_write, run_row, start_run, task_row, task_spec
from polyris.coordination import actions, transitions
from polyris.coordination.deadlines import RowTimes
from polyris.coordination.times import format_utc

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "sam" / "sfn_templates"
COORDINATION = REPO / "sam" / "lambdas" / "coordination"
#: The tokens table's name in the loaded templates: a write to it is a write of a task row or one of its kin.
TOKENS = "the-tokens-table"

# ── 1.3: the writers against the transition table (ADR #137 §3, invariant 7) ──

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
DISPATCHER, START_RUN = transitions.Writer.DISPATCHER, transitions.Writer.START_RUN
EXECUTION = "orders-run-2026-11-19-abcd1234"
RUN = dispatch.RunView(key=run_row.key(EXECUTION), execution=EXECUTION, status=run_row.ACTIVE, version=3,
                       run_start="2026-11-19T06:00:00.123Z", token_order=1, task_count=2)
STATUSES = sorted(s.value for s in TaskStatus)
MOVES = [(move.name, status) for move in transitions.TASK_TRANSITIONS for status in STATUSES]


def _row(status: str) -> dispatch.RowView:
    return dispatch.RowView(task_id="load", key=f"load-2026-11-18-{EXECUTION}", status=status, generation=2,
                            trigger_rule="all_success", dependencies=(), times=RowTimes(status=status))


@pytest.mark.parametrize("name,status", MOVES, ids=[f"{name}:{status}" for name, status in MOVES])
def test_a_pass_moves_a_task_row_only_as_the_table_gives_the_move_to_the_dispatcher(name, status):
    move = transitions.task_transition(name)
    write = lambda: dispatch_writes.task_write(_row(status), dispatch.Judgment("load", name), RUN,  # noqa: E731
                                               table=TOKENS, now=NOW)
    if DISPATCHER not in move.writers or status not in move.sources:
        with pytest.raises(ValueError, match="gives the dispatcher no"):
            write()
        return
    update, check = write()
    values = update["Update"]["ExpressionAttributeValues"]
    names = set(update["Update"]["ExpressionAttributeNames"].values())
    assert update["Update"]["ConditionExpression"] == row_write.ROW_CONDITION       # on what the pass read
    assert task_row.FENCE in update["Update"]["ConditionExpression"]                       # under its generation
    assert (values[":read"], values[":g"], values[":status"]) == ({"S": status}, {"N": "2"}, {"S": move.target})
    assert check == dispatch_writes.version_check(RUN, table=TOKENS)                       # a decision (§1)
    # The table says which moves end an ownership and which skip origin a move records (§3, invariant 6).
    assert (values.get(":g_next") == {"N": "3"}) is move.raises_generation
    assert (task_row.SKIP_ORIGIN in names) is (move.skip_origin is not None)


def test_a_pass_that_keeps_a_rows_status_writes_no_status_and_still_on_what_it_read():
    judgment = dispatch.Judgment("load", fields={task_row.LAST_CHECK: format_utc(NOW), task_row.CHECKS: 1})
    [update] = dispatch_writes.task_write(_row(TaskStatus.RUNNING.value), judgment, RUN, table=TOKENS, now=NOW)
    assert ":status" not in update["Update"]["ExpressionAttributeValues"]
    assert update["Update"]["ConditionExpression"] == row_write.ROW_CONDITION


def _spec(**fields: Any) -> task_spec.TaskSpec:
    base = {"task_id": "t", "task_type": "sfn", "dependencies": [], "trigger_rule": "all_success"}
    text = task_spec.canonical_json({**base, **fields})
    return task_spec.parse(text, expected_hash=task_spec.text_hash(text))


SPECS = {"extract": _spec(task_id="extract"), "audit": _spec(task_id="audit", dependencies=["extract"])}
CALL = start_run.call_from({
    "format_version": task_spec.FORMAT_VERSION, "spec_id": "orders#" + "f" * 64, "task_count": len(SPECS),
    "pipeline_name": "orders", "execution_name": EXECUTION, "execution_arn": f"arn:aws:states:us-east-1:123456789012:execution:orders:{EXECUTION}", "execution_start": RUN.run_start, "token": "token-1",
    "retry_count": 0, "redrive_count": 0, "date": "2026-11-18", "input": {"skip_tasks": ["audit"]}})


def test_start_run_creates_each_task_row_as_the_table_gives_creation_to_start_run():
    items = start_run.task_items(CALL, SPECS, now=NOW)
    assert {item[task_row.STATUS] for item in items} == {TaskStatus.WAITING.value, TaskStatus.SKIPPED.value}
    for item in items:
        assert transitions.allowed(transitions.NO_ROW, item[task_row.STATUS], START_RUN), item[task_row.STATUS]
        if item[task_row.STATUS] == TaskStatus.SKIPPED.value:
            assert item[task_row.SKIP_ORIGIN] == transitions.task_transition("create_skipped").skip_origin
    # It creates a row, or meets one its own earlier attempt wrote (start_run.accept_existing).
    assert start_run.task_condition()["ConditionExpression"] == "attribute_not_exists(#key)"


#: Each writer of the run row: the transitions of the table it makes, and how to build its update. A writer
#: with none keeps the status and run_version: the summary, the run row's due time, and its removal (§3).
RUN_ROW_WRITERS: Dict[str, Tuple[Tuple[str, ...], Callable[[], Mapping[str, Any]]]] = {
    "start_run.run_row_write": (("create", "new_token"), lambda: start_run.run_row_write(CALL, now=NOW)),
    "start_run.activation": (("activate",), lambda: start_run.activation(CALL, now=NOW)),
    "dispatch_writes.answered": (("answer",), lambda: dispatch_writes.answered(RUN, table=TOKENS, now=NOW)),
    "dispatch_writes.stop_done": (("stop_done",), lambda: dispatch_writes.stop_done(RUN, table=TOKENS, now=NOW)),
    "dispatch_writes.engine_stop": (("engine_stop",), lambda: dispatch_writes.engine_stop(
        RUN, table=TOKENS, cause="its execution ended ABORTED before the run's answer", now=NOW)),
    "generators._mark_state": (("mark_step_failure",), lambda: generators._mark_state(
        SimpleNamespace(step_id="notify"), TOKENS, "Next")["Arguments"]),
    "dispatch_writes.summary": ((), lambda: dispatch_writes.summary(
        RUN, dispatch.run_outcome(RUN, {"extract": TaskStatus.RUNNING.value}), table=TOKENS, read_at=NOW)),
    "dispatch_writes.due_now": ((), lambda: dispatch_writes.due_now(RUN, table=TOKENS, now=NOW, left=True)),
    "dispatch_writes.close": ((), lambda: dispatch_writes.close(RUN, table=TOKENS, now=NOW)),
    "dispatch_writes.checked": ((), lambda: dispatch_writes.checked(
        RUN.key, table=TOKENS, due_read=format_utc(NOW), version_read=RUN.version, checks=0, now=NOW)),
    "dispatch_writes.checked, failed": ((), lambda: dispatch_writes.checked(
        RUN.key, table=TOKENS, due_read=format_utc(NOW), version_read=RUN.version, checks=0, now=NOW, failed=True)),
    "dispatch_writes.check_failed": ((), lambda: dispatch_writes.check_failed(
        RUN.key, table=TOKENS, due_read=format_utc(NOW), version_read=RUN.version, now=NOW)),
    "actions.run_raise": (("task_action",), lambda: actions.run_raise(RUN.key, table=TOKENS, now=NOW)),
    "actions.run_request": (("request",), lambda: actions.run_request(
        RUN, {run_row.REQUESTS: {}}, task_id="extract", by="ops", now=NOW, table=TOKENS)),
    "actions.reset": (("reset",), lambda: actions.reset(_reset_row(), dispatch.row_view(_reset_row()), RUN, now=NOW,
                                                        table=TOKENS)[1]["Update"]),
    "actions.clear_request": (("clear_request",), lambda: actions.clear_request(RUN, run_row.POINTERS, table=TOKENS,
                                                                                now=NOW)),
    "actions.stop_run": (("stop",), lambda: actions.stop_run(_run_item(), by="ops", reason="", now=NOW, table=TOKENS)),
    "actions.pause_run": (("pause",), lambda: actions.pause_run(_run_item(), now=NOW, table=TOKENS)),
    "actions.resume_run": (("resume",), lambda: actions.resume_run(
        {**_run_item(), run_row.PAUSES: [[int(NOW.timestamp())]]}, now=NOW, table=TOKENS)),
}


def _run_item() -> Dict[str, Any]:
    return {run_row.TABLE_KEY: RUN.key, run_row.EXECUTION: RUN.execution, run_row.STATUS: run_row.ACTIVE,
            run_row.RUN_VERSION: RUN.version}


def _reset_row() -> Dict[str, Any]:
    return {task_row.TABLE_KEY: "audit-key", task_row.TASK_NAME: "audit", task_row.STATUS: "upstream_failed",
            task_row.GENERATION: 2, task_row.READINESS: task_spec.canonical_json(
                {"trigger_rule": "all_success", "dependencies": ["extract"]})}

#: The run row's transitions whose writer is not built yet (ADR #137 §3, §4): each joins RUN_ROW_WRITERS with its
#: writer.
NOT_BUILT: frozenset = frozenset()


@pytest.mark.parametrize("writer", sorted(RUN_ROW_WRITERS))
def test_every_writer_of_the_run_row_raises_run_version_exactly_when_the_table_says(writer):
    names, build = RUN_ROW_WRITERS[writer]
    expression = str(build()["UpdateExpression"])
    raises = run_row.VERSION_RAISE in expression or run_row.VERSION_NEXT in expression
    assert {transitions.run_transition(name).raises_version for name in names} | ({False} if not names else set()) \
        == {raises}, writer
    if not any(transitions.run_transition(name).target for name in names):
        assert "#status" not in expression, f"{writer} moves the run row's status, which the table does not give it"


def test_the_writers_that_move_the_run_rows_status_move_it_to_the_tables_target():
    created = start_run.run_row_write(CALL, now=NOW)
    assert "#status = if_not_exists(#status, :creating)" in created["UpdateExpression"]
    assert created["ExpressionAttributeValues"][":creating"] == transitions.run_transition("create").target
    activated = start_run.activation(CALL, now=NOW)
    assert activated["ConditionExpression"].startswith("#status = :creating")
    assert activated["ExpressionAttributeValues"][":active"] == transitions.run_transition("activate").target


def test_every_transition_of_the_run_row_has_its_writer_here_or_is_not_built_yet():
    covered = {name for names, _ in RUN_ROW_WRITERS.values() for name in names}
    assert covered | NOT_BUILT == {t.name for t in transitions.RUN_TRANSITIONS}
    assert not covered & NOT_BUILT, covered & NOT_BUILT


def test_the_coordination_lambdas_write_task_rows_and_the_run_row_only_through_the_builders():
    """The dispatcher and Start_Run build no update and no condition of their own (``dispatch_writes``,
    ``start_run``, and ``actions`` for the requests a pass carries out): what they send is what the tests above
    check."""
    for module in ("dispatcher.py", "start_run.py"):
        text = (COORDINATION / module).read_text()
        assert "UpdateExpression" not in text and "ConditionExpression" not in text, module
    sends = [node for node in ast.walk(ast.parse((COORDINATION / "dispatcher.py").read_text()))
             if isinstance(node, ast.Call) and ast.unparse(node.func) == "self._send"]
    assert sends, "the scan of the dispatcher's sends is broken"
    for node in sends:
        assert isinstance(node.args[0], ast.Call) and ast.unparse(node.args[0].func).startswith(
            ("dispatch_writes.", "actions.")), \
            ast.unparse(node)


# ── 1.3: run_task's writes of its row (ADR #137 §1, invariant 5) ────────────

RUN_TASK = "helpers/run_task"
#: Each write run_task makes of its row: the move it makes, or None for a write of fields alone.
RUN_TASK_WRITES = {"Claim": "run", "Record_Success": "succeed", "Ask": "ask",
                   "Save_Retry_Wait_Delay": None, "Save_Retry_Attempt": None, "Save_Slack_Failed": None}


@functools.lru_cache(maxsize=None)
def _run_task_row_writes() -> Dict[str, dict]:
    """run_task's updates of its row: its updateItem states, and the row's item of the success transaction."""
    from sfn_jsonata import evaluate
    states = load_template(f"{RUN_TASK}/sfn.tpl.json", {"tokens_table": TOKENS})["States"]
    found = {name: state["Arguments"] for name, state in _writes(states)
             if state["Arguments"].get("TableName") == TOKENS
             and "Input.execution_name" in json.dumps(state["Arguments"].get("Key", {}))}
    run = {"execution_name": "load-2026-11-19-x", "generation": 2, "task_name": "load", "pipeline_name": "orders",
           "date": "2026-11-19", "cascade": "auto", "outlets": [], "output_ttl_seconds": 1, "history_ttl_seconds": 1}
    items = evaluate(states["Record_Success"]["Arguments"], input={"task_output": "{}", "_task_pushed": False},
                     context={"Execution": {"Input": run, "Id": "arn", "Name": "n"}})["TransactItems"]
    [row] = [i["Update"] for i in items if i["Update"]["Key"]["execution_name"]["S"] == run["execution_name"]]
    found["Record_Success"] = row
    return found


def test_run_task_writes_its_row_only_as_listed():
    assert set(_run_task_row_writes()) == set(RUN_TASK_WRITES)


@pytest.mark.parametrize("name", sorted(RUN_TASK_WRITES))
def test_run_task_writes_its_row_only_under_its_generation_and_as_the_table_gives_it_the_move(name):
    """Every write is conditional on the status run_task expects and on its generation (task_row.FENCE); a
    write that moves the row is a move the table gives run_task, from the status it is conditional on."""
    arguments = _run_task_row_writes()[name]
    condition, values = arguments["ConditionExpression"], arguments["ExpressionAttributeValues"]
    names = arguments["ExpressionAttributeNames"]
    expected, fence = condition.split(" AND ")
    assert fence == task_row.FENCE and names["#g"] == task_row.GENERATION, name
    left, right = expected.split(" = ")
    assert names[left] == task_row.STATUS, name
    read = values[right]["S"]
    move = RUN_TASK_WRITES[name]
    sets_status = "#s = " in arguments["UpdateExpression"].split("REMOVE")[0]
    if move is None:
        assert not sets_status and read in (TaskStatus.RUNNING.value, TaskStatus.WAITING_DECISION.value), name
        return
    transition = transitions.task_transition(move)
    assert transitions.Writer.RUN_TASK in transition.writers and read in transition.sources, name
    target = arguments["UpdateExpression"].split("#s = ", 1)[1].split(",")[0].strip()
    assert values[target] == {"S": transition.target}, name
    assert not transition.raises_generation and "#g =" not in arguments["UpdateExpression"], name


def test_every_move_the_table_gives_run_task_has_its_write():
    moves = {t.name for t in transitions.TASK_TRANSITIONS if transitions.Writer.RUN_TASK in t.writers}
    assert moves == {m for m in RUN_TASK_WRITES.values() if m}


# ── templates other than run_task: none writes a task row (TR-8) ──────

#: A key that starts with one of these is a row of its own kind, not a task row.
OTHER_ROWS = ("_notify_warn_", "_pause_", "_retry_push_", "output#", "input#")

#: A key built from one of these input fields names another kind of row.
OTHER_KEYS = ("backfill_id",)       # the bulk backfill machine's own record


def _writes(states: dict) -> Iterator[Tuple[str, dict]]:
    for name, state in states.items():
        resource = str(state.get("Resource", ""))
        if "dynamodb" in resource and resource.rsplit(":", 1)[-1] in ("putItem", "updateItem", "deleteItem"):
            yield name, state
        for branch in state.get("Branches", []) or []:
            yield from _writes(branch["States"])
        for key in ("ItemProcessor", "Iterator"):
            if key in state:
                yield from _writes(state[key]["States"])


def _template_task_row_writes() -> Dict[Tuple[str, str], dict]:
    found = {}
    for path in sorted(TEMPLATES.rglob("sfn.tpl.json")):
        machine = str(path.parent.relative_to(TEMPLATES))
        if machine == RUN_TASK:
            continue                                        # 1.3's writer: the census above
        states = load_template(str(path.relative_to(TEMPLATES)), {"tokens_table": TOKENS})["States"]
        for name, state in _writes(states):
            arguments = state.get("Arguments", {})
            if arguments.get("TableName") != TOKENS:
                continue                                    # another table: facts, task events, registry, ...
            key = (arguments.get("Key") or arguments.get("Item") or {}).get("execution_name", {}).get("S", "")
            if any(prefix in key for prefix in OTHER_ROWS) or any(field in key for field in OTHER_KEYS):
                continue
            found[(machine, name)] = arguments
    return found


TEMPLATE_WRITES = _template_task_row_writes()


def test_no_template_but_run_task_writes_a_task_row():
    """run_task has its own census above. The 1.2 templates that wrote task rows (the wrapper, its registration,
    failure handler, pause waiter and restart helper) are gone (TR-8): a new template that writes one needs the
    generation fence (Rule 47) and its place in a census here."""
    assert TEMPLATE_WRITES == {}


# ── every write call of the code, and its role ──────────────────────────

#: Code that writes the tokens table, and every write call of the coordination Lambdas whatever its table,
#: by (file, function), with the role of each write.
CODE_WRITERS = {
    ("console_api/routes/tasks.py", "_transact"):
        "an operator's action on a task: actions.task_action and actions.restart, whose row_write raises the "
        "generation as the table says",
    ("console_api/routes/executions.py", "_run_action"):
        "the run row: actions.stop_run, pause_run and resume_run (a Stop of the run, Pause, Resume)",
    ("coordination/dispatcher.py", "_send_items"): "task rows and the run row's check: dispatch_writes.task_write",
    ("coordination/dispatcher.py", "_send"): "the run row: dispatch_writes (answer, summary, stop, due time)",
    ("coordination/dispatcher.py", "_drop_pointers"): "another table: a row's pointers once it left its wait",
    ("coordination/dispatcher.py", "_put_pointers"): "another table: a waiting row's pointers (actions.pointer_items)",
    ("coordination/dispatcher.py", "_put_warning"): "warning rows (records.warning_item)",
    ("coordination/dispatcher.py", "_timeline"): "another table: a decision's timeline event (records.decision_event)",
    ("coordination/start_run.py", "_start"): "another table: the asset: pointers (start_run.pointer_items)",
    ("coordination/start_run.py", "_write_run_row"): "the run row: start_run.run_row_write",
    ("coordination/start_run.py", "_put"): "task rows and the run's input: start_run.task_items, input_item",
    ("coordination/start_run.py", "_activate"): "the run row: start_run.activation",
    ("coordination/sweeper.py", "_move"): "the run row's due time and the mark of a failed check: dispatch_writes.checked, "
                                          "check_failed",
    ("coordination/sweeper.py", "_put_warning"): "warning rows (records.warning_item)",
    ("coordination/sweeper.py", "_take_lease"): "the sweeper's lease row",
    ("coordination/sweeper.py", "_release_lease"): "the sweeper's lease row",
    ("coordination/sweeper.py", "_retry_again"): "push retry rows",
    ("coordination/sweeper.py", "_drop"): "push retry rows",
    ("coordination/sweeper.py", "_save_catch_up"): "the engine health row",
    ("coordination/sweeper.py", "_save_health"): "the engine health row",
    ("coordination/asset_fanout.py", "_run_of"): "another table: a pointer whose row is gone",
    ("coordination/asset_fanout.py", "_drop_subscription"): "another table: a push consumer's subscription",
    ("coordination/asset_fanout.py", "_retry_later"): "push retry rows",
    ("coordination/asset_fanout.py", "_warn"): "warning rows (records.warning_item)",
    ("console_api/routes/tasks.py", "_emit_marker_blocked_warn"): "warning rows",
    ("console_api/routes/tasks.py", "_warn_run_task_left_running"): "warning rows (records.warning_item)",
}

#: The paid overlay, when it is checked out next to this repository: its writers are classified too, under
#: ``ee/`` (the final review of TR-6, P-24; test_repository_calls.py reads it the same way).
OVERLAY = REPO.parent / "polyris-ee" / "sam" / "lambdas" / "console_api" / "ee"
CODE_FILES = (sorted((REPO / "sam/lambdas/console_api/routes").glob("*.py"))
              + [REPO / "sam/lambdas/console_api/utils.py"]
              + sorted(COORDINATION.glob("*.py"))
              + (sorted(p for p in OVERLAY.rglob("*.py") if "tests" not in p.parts) if OVERLAY.is_dir() else []))


def _name(path: Path) -> str:
    """A file's name in CODE_WRITERS: from sam/lambdas here, from ``ee/`` in the paid overlay."""
    if OVERLAY in path.parents:
        return str(Path("ee") / path.relative_to(OVERLAY))
    return str(path.relative_to(REPO / "sam/lambdas"))
#: The DynamoDB write calls; in the coordination Lambdas every one of them is classified, whatever its table.
WRITE_CALLS = ("put_item", "update_item", "delete_item", "transact_write_items", "batch_write_item")


def _code_writers() -> Dict[Tuple[str, str], str]:
    found = {}
    for path in CODE_FILES:
        tree = ast.parse(path.read_text())
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                target, method = ast.unparse(node.func.value), node.func.attr
                repo_write = target == "executions_repo" and method in ("update", "put", "delete", "transact_write")
                table_write = method in ("update_item", "put_item", "delete_item") and (
                    "tasks" in target or target == "self.table")
                coordination_write = path.parent == COORDINATION and method in WRITE_CALLS
                if repo_write or table_write or coordination_write:
                    found[(_name(path), function.name)] = ast.get_source_segment(
                        path.read_text(), function) or ""
    return found


CODE = _code_writers()


def test_the_census_reads_the_paid_overlay_when_it_is_checked_out():
    """A writer of task rows in the overlay is classified like one here (P-24)."""
    if not OVERLAY.is_dir():
        pytest.skip("the paid overlay is not checked out next to this repository")
    assert any(OVERLAY in path.parents for path in CODE_FILES)


def test_every_code_writer_has_a_role():
    unknown = sorted(set(CODE) - set(CODE_WRITERS))
    assert not unknown, f"classify these writers in CODE_WRITERS: {unknown}"
    gone = sorted(set(CODE_WRITERS) - set(CODE))
    assert not gone, f"these writers no longer exist; drop them from CODE_WRITERS: {gone}"
