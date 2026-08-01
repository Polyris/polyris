#!/usr/bin/env python3
"""Exhaustive JSONata edge-case audit for SFN templates (ADR #85).

Evaluates every ``{% %}`` JSONata expression across every ``.tpl.json``
template under ``sam/sfn_templates/`` against a battery of input variants —
including aggressive edge cases (missing fields, null, wrong types, empty
collections, unicode, special characters, populated arrays, error-as-object
vs string) — and reports any expression that throws a type/signature error
(JSONata T0410 / T0411 / T0412 / T0413).

A throw here = ``States.QueryEvaluationError`` at runtime = a failed task
in production. The skip-task ``$contains``-on-array bug fixed in v0.80.1
(ADR #85) was exactly this class of failure; this script is the standing
audit that would have caught it.

This is a **maintenance tool**, not a CI gate — input synthesis is not a
perfect mirror of ASL runtime ($states.context intrinsics, etc.), so the
output is "no false negatives, may have false positives" and should be
read by a human. Run before releases or after non-trivial template
changes:

    python scripts/audit_jsonata.py

or:

    make audit-jsonata

Exits 0 if zero throws across all variants, 1 otherwise.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterator

REPO = Path(__file__).resolve().parent.parent
TPL_DIR = REPO / "sam" / "sfn_templates"

# T-codes JSONata raises for type/signature mismatches. A throw with one of
# these markers in a Choice condition or Output expression means the state
# fails with States.QueryEvaluationError at runtime.
TYPE_MARKERS = (
    "signature", "does not match", "is not of type", "not of type",
    "T0410", "T0411", "T0412", "T0413",
)

# SAM ``${name}`` placeholders are not valid JSON; substitute a value that
# parses both in numeric and string contexts.
PLACEHOLDER_RE = re.compile(r"\$\{[^}]+\}")


def _strip_placeholders(text: str) -> str:
    return PLACEHOLDER_RE.sub("1", text)


# ──────────────────────────────────────────────────────────────────────────
# Expression extraction
# ──────────────────────────────────────────────────────────────────────────

EXPR_RE = re.compile(r"\{%(.*?)%\}", re.S)

EXPR_ROLES = (
    "Arguments", "Output", "Assign", "Items", "ItemSelector",
    "MaxConcurrencyPath",
)


def _yield_exprs(state_path: str, role: str, value) -> Iterator[tuple]:
    body = value if isinstance(value, str) else json.dumps(value)
    for m in EXPR_RE.finditer(body):
        yield (state_path, role, "{%" + m.group(1) + "%}")


def walk_states(states: dict, parent: str = "") -> Iterator[tuple]:
    """Yield (state_path, role, expression) for every {% %} found."""
    for name, state in states.items():
        if not isinstance(state, dict):
            continue
        path = f"{parent}/{name}" if parent else name
        for role in EXPR_ROLES:
            if role in state:
                yield from _yield_exprs(path, role, state[role])
        for choice in state.get("Choices", []):
            cond = choice.get("Condition")
            if isinstance(cond, str) and "{%" in cond:
                yield (path, "ChoiceCondition", cond)
        for catch in state.get("Catch", []):
            if "Output" in catch:
                yield from _yield_exprs(path, "CatchOutput", catch["Output"])
        for nested_key in ("ItemProcessor", "Iterator"):
            if nested_key in state:
                yield from walk_states(state[nested_key].get("States", {}), path)
        for branch in state.get("Branches", []):
            yield from walk_states(branch.get("States", {}), path)


# ──────────────────────────────────────────────────────────────────────────
# Input synthesis — base + edge-case variants
# ──────────────────────────────────────────────────────────────────────────

def _base_input() -> dict:
    return {
        "task_name": "stage_catalog", "pipeline_name": "acme-daily",
        "pipeline_execution": "acme-daily-2026-05-20",
        "pipeline_execution_short": "20260520",
        "status": "success", "task_type": "lambda", "cascade": "auto",
        "current_date": "2026-05-20", "partition_key": "2026-05-20",
        "backfill_id": "bf-336b8481", "is_backfill": True, "attempt": 1,
        "orchestration_timeout": 3600, "ttl": 123, "token": "tk",
        "date": "2026-05-20", "skip_tasks": [], "dependencies": [],
        "outlets": [], "wait_for": [], "error": "boom",
        "task_output": '{"rows":5}', "task_config": {"k": "v"},
        "trigger_rule": "all_success", "cross_account_role": "same",
        "slack_channel": "#x", "has_slack": True, "paused": False,
        "cancelled": False,
        "execution_name": "stage_catalog-2026-05-20-20260520",
        "task_run_id": "r1", "skip_task_ids": [], "variables": {},
        "target_pipeline": "acme-daily",
        "target_pipeline_sfn_arn": "arn:aws:states:::sfn",
        "slack_mentions": "", "wrapper_execution_arn": "arn:aws:wrapper",
        "parent_execution_id": "pe", "orchestration_token": "ot",
        "task_execution_arn": "arn:t", "subscribers": [],
        "fan_out_dependents": [], "task_id": "stage_catalog",
        "should_skip": False, "last_event_id": 0,
    }


# (label, overrides) — overrides with value None mean "delete that key"
VARIANTS_BASELINE = [
    ("baseline", {}),
    ("non-empty skip_tasks", {"skip_tasks": ["stage_catalog", "x"]}),
    ("skip no match", {"skip_tasks": ["a", "b"]}),
    ("task_output as object", {"task_output": {"rows": 5}}),
    ("task_output absent", {"task_output": None}),
    ("error as object", {"error": {"Error": "E", "Cause": "C"}}),
    ("error with json Cause", {"error": {"Error": "E", "Cause": '{"ExecutionArn":"arn:foo"}'}}),
    ("dependencies populated", {"dependencies": [{"task": "a", "status": "success"}]}),
    ("outlets populated", {"outlets": ["asset1", "asset2"]}),
    ("wait_for populated", {"wait_for": [{"task": "x"}]}),
    ("long pipeline name", {"pipeline_execution": "x" * 100}),
    ("status failed", {"status": "failed"}),
    ("paused true", {"paused": True}),
    ("absent skip_tasks", {"skip_tasks": None}),
    ("subscribers populated", {"subscribers": [{"sub": "s1"}]}),
]

VARIANTS_AGGRESSIVE = [
    ("status null", {"status": None}),
    ("paused null", {"paused": None}),
    ("cascade unknown", {"cascade": "unknown_value"}),
    ("task_type unknown", {"task_type": "mystery"}),
    ("attempt as string", {"attempt": "1"}),
    ("attempt zero", {"attempt": 0}),
    ("attempt negative", {"attempt": -1}),
    ("ttl as string", {"ttl": "9999"}),
    ("orchestration_timeout 0", {"orchestration_timeout": 0}),
    ("outlets null", {"outlets": None}),
    ("outlets as object", {"outlets": {"wrong": "type"}}),
    ("dependencies null", {"dependencies": None}),
    ("variables as list", {"variables": []}),
    ("current_date missing", {"current_date": None}),
    ("task_name empty", {"task_name": ""}),
    ("error empty string", {"error": ""}),
    ("long task_name", {"task_name": "x" * 200}),
    ("special chars task_name", {"task_name": "name'with\"quotes\nand\\backslash"}),
    ("unicode task_name", {"task_name": "таска_кирилицею_🚀"}),
    ("trigger_rule unknown", {"trigger_rule": "mystery_rule"}),
]

ALL_VARIANTS = VARIANTS_BASELINE + VARIANTS_AGGRESSIVE


def _build_frame(jsonata_mod, overrides: dict):
    """Build a JSONata frame with $states bound for the given input variant."""
    inp = _base_input()
    for key, value in overrides.items():
        if value is None and key in inp:
            del inp[key]
        else:
            inp[key] = value
    states = {
        "input": inp,
        "context": {
            "Execution": {"Id": "arn:exec", "Name": "exec-name",
                          "StartTime": "2026-01-01T00:00:00Z", "Input": {}},
            "State": {"Name": "s", "RetryCount": 0,
                      "EnteredTime": "2026-01-01T00:00:00Z"},
            "StateMachine": {"Id": "arn:sm", "Name": "sm"},
            "Task": {"Token": "t"},
        },
        "result": {
            "Output": {"rows": 5}, "Payload": {"p": 1},
            "ExecutionArn": "arn:r", "StateMachineArn": "arn:rsm",
            "JobRunId": "jr1", "Tasks": [{"TaskArn": "arn:tk"}],
            "QueryExecutionId": "qe", "StepId": "st", "JobId": "jb",
            "Status": "SUCCEEDED", "Cause": "C", "Error": "E",
        },
        "errorOutput": {"Error": "TaskFailed",
                        "Cause": '{"ExecutionArn":"arn:c"}'},
    }
    j = jsonata_mod.Jsonata("$states")  # any expr; we only need the frame
    frame = j.create_frame()
    frame.bind("states", states)
    return frame


# ──────────────────────────────────────────────────────────────────────────
# Audit
# ──────────────────────────────────────────────────────────────────────────

def audit() -> int:
    try:
        import jsonata
    except ImportError:
        print("ERROR: jsonata-python is not installed. Run: pip install -e '.[dev]'",
              file=sys.stderr)
        return 2

    all_exprs = []
    for tpl in sorted(TPL_DIR.rglob("*.tpl.json")):
        try:
            data = json.loads(_strip_placeholders(tpl.read_text()))
        except json.JSONDecodeError as e:
            print(f"WARN: cannot parse {tpl}: {e}", file=sys.stderr)
            continue
        for path, role, expr in walk_states(data.get("States", {})):
            all_exprs.append((tpl.relative_to(REPO).as_posix(), path, role, expr))

    unique: dict[str, list] = {}
    for tpl, path, role, expr in all_exprs:
        unique.setdefault(expr, []).append((tpl, path, role))

    print(f"Templates scanned: {sum(1 for _ in TPL_DIR.rglob('*.tpl.json'))}")
    print(f"Total {{% %}} expressions: {len(all_exprs)} ({len(unique)} unique)")
    print(f"Variants: {len(ALL_VARIANTS)}")
    print(f"Evaluations: {len(unique) * len(ALL_VARIANTS):,}")
    print()

    throwers: dict[str, set] = defaultdict(set)

    for expr, locs in unique.items():
        body = expr.strip()
        if body.startswith("{%"):
            body = body[2:]
        if body.endswith("%}"):
            body = body[:-2]
        body = body.strip()
        if not body:
            continue
        try:
            compiled = jsonata.Jsonata(body)
        except Exception as exc:
            throwers[expr].add(("PARSE_ERROR", str(exc)[:80]))
            continue
        for variant_name, overrides in ALL_VARIANTS:
            try:
                compiled.evaluate(None, _build_frame(jsonata, overrides))
            except Exception as exc:
                msg = str(exc)
                if any(marker in msg for marker in TYPE_MARKERS):
                    throwers[expr].add((variant_name, msg[:80]))

    if not throwers:
        print("✅ No type/signature throws found across all expressions × variants.")
        return 0

    print(f"⚠️  {len(throwers)} expression(s) threw type/signature errors:")
    for expr, problems in throwers.items():
        locs = unique[expr]
        print(f"\n  [{len(locs)} site(s)] first: {locs[0][0]}")
        print(f"    state: {locs[0][1]} / {locs[0][2]}")
        snippet = expr[:140] + ("..." if len(expr) > 140 else "")
        print(f"    expr: {snippet}")
        for variant, msg in sorted(problems):
            print(f"      [{variant}] {msg}")
    return 1


if __name__ == "__main__":
    sys.exit(audit())
