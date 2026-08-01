"""Evaluate the SFN skip-task JSONata conditions against real inputs (ADR #85).

The bulk_backfill task-subset feature (ADR #51) passes a ``skip_tasks`` array
into each task's run-task-helper / dependency-wrapper execution; a Choice
state decides whether to skip the task. A regression shipped a condition that
used ``$contains($states.input.skip_tasks, $states.input.task_name)`` —
``$contains`` is a STRING function, so passing the ``skip_tasks`` ARRAY threw
``States.QueryEvaluationError`` (T0410) at runtime the moment ``skip_tasks``
was non-empty, failing the task and cascading skips downstream. Nothing in CI
caught it because SFN-template JSONata was never evaluated.

This test evaluates every skip-task Choice condition (any condition that
references ``skip_tasks``) across the SFN templates against representative
inputs, asserting it never throws and returns the correct membership boolean.
The correct idiom is the ``in`` operator: ``task_name in skip_tasks``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES_DIR = REPO_ROOT / "sam" / "sfn_templates"

# (description, skip_tasks, task_name, expected membership result)
CASES = [
    ("empty list",            [],                          "stage_catalog", False),
    ("single-element match",  ["stage_catalog"],           "stage_catalog", True),
    ("multi-element match",   ["extract", "stage_catalog"], "stage_catalog", True),
    ("multi-element no match", ["a", "b"],                  "stage_catalog", False),
    ("absent skip_tasks",     None,                         "stage_catalog", False),
]


def _strip_placeholders(text: str) -> str:
    """Replace SAM ${...} placeholders so the .tpl.json parses as JSON.

    Skip-task conditions contain no placeholders, but other parts of the
    template do; substitute a harmless string so json.loads succeeds.
    """
    return re.sub(r"\$\{[^}]+\}", "1", text)


def _iter_choice_conditions(states: dict):
    """Yield every Choice condition string in a (possibly nested) States map."""
    for state in states.values():
        if not isinstance(state, dict):
            continue
        if state.get("Type") == "Choice":
            for choice in state.get("Choices", []):
                cond = choice.get("Condition")
                if isinstance(cond, str):
                    yield cond
        # Map / Parallel nesting
        if "ItemProcessor" in state:
            yield from _iter_choice_conditions(state["ItemProcessor"].get("States", {}))
        if "Iterator" in state:
            yield from _iter_choice_conditions(state["Iterator"].get("States", {}))
        for branch in state.get("Branches", []):
            yield from _iter_choice_conditions(branch.get("States", {}))


def _skip_task_conditions():
    """All Choice conditions across templates that reference skip_tasks."""
    found = []
    for tpl in TEMPLATES_DIR.rglob("*.tpl.json"):
        data = json.loads(_strip_placeholders(tpl.read_text()))
        for cond in _iter_choice_conditions(data.get("States", {})):
            if "skip_tasks" in cond:
                found.append((tpl.relative_to(REPO_ROOT).as_posix(), cond))
    return found


def _evaluate(condition: str, skip_tasks, task_name):
    """Evaluate a `{% ... %}` JSONata condition with $states bound."""
    jsonata = pytest.importorskip(
        "jsonata",
        reason="jsonata-python is a dev dependency (pip install -e '.[dev]')",
    )
    expr = condition.strip()
    if expr.startswith("{%"):
        expr = expr[2:]
    if expr.endswith("%}"):
        expr = expr[:-2]
    j = jsonata.Jsonata(expr.strip())
    frame = j.create_frame()
    states_input = {"task_name": task_name}
    if skip_tasks is not None:
        states_input["skip_tasks"] = skip_tasks
    frame.bind("states", {"input": states_input})
    return j.evaluate(None, frame)


def test_skip_task_conditions_exist():
    """Guard the guard: at least the run_task + dependency_wrapper checks."""
    conds = _skip_task_conditions()
    assert len(conds) >= 2, f"expected >=2 skip-task conditions, found {conds}"


@pytest.mark.parametrize("desc,skip_tasks,task_name,expected", CASES)
def test_skip_task_jsonata_never_throws_and_is_correct(desc, skip_tasks, task_name, expected):
    """Every skip-task condition must evaluate cleanly to the right boolean.

    Catches the ADR #51 regression: $contains() on the skip_tasks array threw
    States.QueryEvaluationError for any non-empty list.
    """
    conditions = _skip_task_conditions()
    assert conditions, "no skip-task conditions found to validate"
    for path, cond in conditions:
        try:
            result = _evaluate(cond, skip_tasks, task_name)
        except Exception as e:  # noqa: BLE001 - we assert it never throws
            pytest.fail(
                f"{path}: skip-task JSONata threw on {desc} "
                f"(skip_tasks={skip_tasks}): {e}\n  condition: {cond}"
            )
        assert bool(result) == expected, (
            f"{path}: skip-task condition returned {result!r} "
            f"(expected {expected}) for {desc}\n  condition: {cond}"
        )


def test_no_contains_on_skip_tasks_array():
    """Static belt-and-suspenders: $contains() must never wrap skip_tasks
    (it is a string function; use the `in` operator for array membership)."""
    offenders = []
    for tpl in TEMPLATES_DIR.rglob("*.tpl.json"):
        for cond in _iter_choice_conditions(
            json.loads(_strip_placeholders(tpl.read_text())).get("States", {})
        ):
            if re.search(r"\$contains\(\s*\$states\.input\.skip_tasks", cond):
                offenders.append((tpl.name, cond))
    assert not offenders, f"$contains() misused on skip_tasks array: {offenders}"
