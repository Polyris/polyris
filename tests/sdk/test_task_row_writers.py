"""Every writer of a task row, and the guard it uses (ADR #130 §2.1, D40).

A task row has one owner at a time: the wrapper holding its generation. A write
made for an owner is fenced by the generation; a write that ends an ownership
raises it; the reconciler decides from what it read. This census finds the
writers in the state-machine templates and in the code, so a new one fails here
until it is given one of these roles.
"""

import ast
from pathlib import Path
from typing import Dict, Iterator, Tuple

import pytest
from sfn_jsonata import load_template

from polyris.coordination import ownership

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "sam" / "sfn_templates"
#: The tokens table's name in the loaded templates: a write to it is a write of a task row or one of its kin.
TOKENS = "the-tokens-table"

#: A key that starts with one of these is a row of its own kind, not a task row.
OTHER_ROWS = ("_notify_warn_", "_pause_", "_retry_push_", "output#", "input#")

#: The first write of a new owner: it may create the row, so it cannot require one.
FIRST_WRITES = {("helpers/registration", "Save_Task_Record"), ("dependency_wrapper", "Auto_Skip_Register")}

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


def test_the_census_finds_the_writers_it_should():
    assert {("dependency_wrapper", f"Claim_{c}") for c in ("Deps_Ready", "Skipped", "Upstream_Failed")} <= set(TEMPLATE_WRITES)
    assert {("helpers/run_task", "Update_Status_Running"), ("helpers/pause_waiter", "Save_Pause_Token"),
            ("helpers/failure_handler", "Update_Status_Failed"), ("helpers/restart_task", "Hand_Over_Ownership"),
            ("helpers/registration", "Save_Task_Record")} <= set(TEMPLATE_WRITES)


@pytest.mark.parametrize("writer", sorted(TEMPLATE_WRITES), ids=lambda w: f"{w[0]}:{w[1]}")
def test_every_state_machine_write_of_a_task_row_is_fenced_or_raises(writer):
    arguments = TEMPLATE_WRITES[writer]
    update, condition = arguments.get("UpdateExpression", ""), arguments.get("ConditionExpression", "")
    if ownership.RAISE in update:
        assert set(ownership.RAISE_VALUES) <= set(arguments["ExpressionAttributeValues"]), writer
        return
    if writer in FIRST_WRITES:
        assert "generation = :g" in condition and arguments["Item"][ownership.FIELD], writer
        return
    assert ownership.FENCE in condition, f"{writer} writes a task row without the generation fence: {condition!r}"
    assert ":g" in arguments["ExpressionAttributeValues"], writer


#: Code that writes the tokens table, by (file, function), and the role of each write.
RAISE = "ends an ownership: raises the generation"
CODE_WRITERS = {
    ("console_api/routes/tasks.py", "_execute_task_action"): RAISE,
    ("console_api/routes/tasks.py", "stop_task"): RAISE,
    ("console_api/routes/executions.py", "_mark_pending_tasks_stopped"): RAISE,
    ("coordination/stores.py", "_close"): RAISE,
    ("coordination/stores.py", "park"): "decides from what it read: wait_token, started_at and status",
    ("coordination/stores.py", "clear_attention"): "attention bookkeeping, conditioned on the status it read",
    ("coordination/stores.py", "mark_due"): "attention bookkeeping, conditioned on started_at",
    ("coordination/stores.py", "mark_pause_due"): "attention bookkeeping, conditioned on the pause token",
    ("coordination/stores.py", "drop_pause_check"): "attention bookkeeping, conditioned on the pause token",
    ("coordination/stores.py", "put"): "warning rows (records.warning_item)",
    ("coordination/stores.py", "delete"): "push retry rows",
    ("coordination/sweeper.py", "_adopt_task"): "adds an attention entry to a row an older version wrote; no status",
    ("coordination/sweeper.py", "_take_lease"): "the sweeper's lease row",
    ("coordination/sweeper.py", "_release_lease"): "the sweeper's lease row",
    ("coordination/sweeper.py", "_retry_again"): "push retry rows",
    ("coordination/sweeper.py", "_drop"): "push retry rows",
    ("coordination/sweeper.py", "_save_catch_up"): "the engine health row",
    ("coordination/sweeper.py", "_save_health"): "the engine health row",
    ("coordination/asset_fanout.py", "_retry_later"): "push retry rows",
    ("console_api/routes/executions.py", "pause_execution"): "the run's pause row",
    ("console_api/routes/executions.py", "resume_execution"): "the run's pause row",
    ("console_api/routes/tasks.py", "_write_notify_warning"): "warning rows",
    ("console_api/routes/tasks.py", "_emit_marker_blocked_warn"): "warning rows",
    ("console_api/routes/tasks.py", "_warn_fact_not_recorded"): "warning rows",
    ("console_api/routes/tasks.py", "_write_synthetic_output_marker"): "the output# row",
}

CODE_FILES = (sorted((REPO / "sam/lambdas/console_api/routes").glob("*.py"))
              + [REPO / "sam/lambdas/console_api/task_actions.py", REPO / "sam/lambdas/console_api/utils.py"]
              + sorted((REPO / "sam/lambdas/coordination").glob("*.py")))


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
                repo_write = target == "executions_repo" and method in ("update", "put", "delete")
                table_write = method in ("update_item", "put_item", "delete_item") and (
                    "tasks" in target or target == "self.table")
                if repo_write or table_write:
                    found[(str(path.relative_to(REPO / "sam/lambdas")), function.name)] = ast.get_source_segment(
                        path.read_text(), function) or ""
    return found


CODE = _code_writers()


def test_every_code_writer_of_the_tokens_table_has_a_role():
    unknown = sorted(set(CODE) - set(CODE_WRITERS))
    assert not unknown, f"classify these writers in CODE_WRITERS (ADR #130 §2.1): {unknown}"
    gone = sorted(set(CODE_WRITERS) - set(CODE))
    assert not gone, f"these writers no longer exist; drop them from CODE_WRITERS: {gone}"


@pytest.mark.parametrize("writer", sorted(w for w, role in CODE_WRITERS.items() if role == RAISE),
                         ids=lambda w: f"{w[0]}:{w[1]}")
def test_every_decision_raises_the_generation_in_the_same_write(writer):
    assert "ownership.RAISE" in CODE[writer] and "ownership.RAISE_VALUES" in CODE[writer], writer
