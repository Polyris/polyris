"""The timeline events of a pass's decisions (ADR #137 §1), as 1.2's wrapper wrote them.

The console reads them by name (``console_api/constants.py::EventType``), and the
dispatcher writes them after the transitions that ``records.DECISION_EVENTS`` names.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from polyris.coordination import records, retention, transitions

NOW = datetime(2026, 11, 21, 6, 0, tzinfo=timezone.utc)
CONSOLE_CONSTANTS = Path(__file__).resolve().parents[2] / "sam" / "lambdas" / "console_api" / "constants.py"


def _event(transition, **extra):
    return records.decision_event(transition=transition, task_run_id="load-x-1a2b", execution_name="load-2026-11-21-x",
                                  task_name="load", pipeline_execution="orders-run-x", partition_key="2026-11-21",
                                  backfill_id="", now=NOW, **extra)


def test_a_ready_rows_event_names_its_dependencies():
    event = _event("ready", dependencies=["extract", "audit"])
    assert event == {"task_run_id": "load-x-1a2b", "event_time": "2026-11-21T06:00:00.000Z#05",
                     "event_type": "DEPS_READY", "execution_name": "load-2026-11-21-x",
                     "parent_execution_id": "orders-run-x", "backfill_id": "", "partition_key": "2026-11-21",
                     "task_name": "load", "dependencies": json.dumps(["extract", "audit"]),
                     "ttl": retention.ttl(NOW, retention.TIMELINE)}


@pytest.mark.parametrize("transition,default", [("skip_by_rule", "Trigger rule condition did not occur"),
                                                ("block", "Upstream task failed")])
def test_a_skip_or_a_block_carries_its_reason(transition, default):
    assert _event(transition, reasons=["1 upstreams failed"])["reason"] == "1 upstreams failed"
    assert _event(transition)["reason"] == default                    # what 1.2's wrapper wrote without one
    assert "dependencies" not in _event(transition)


def test_the_decision_events_follow_the_dispatchers_transitions_and_the_consoles_names():
    """Principle #28: the names cross from the dispatcher to the console's timeline."""
    body = re.search(r"class EventType:(.*?)(?=\nclass |\Z)", CONSOLE_CONSTANTS.read_text(), re.DOTALL).group(1)
    known = set(re.findall(r"'([A-Z_]+)'", body))
    assert set(records.DECISION_EVENTS.values()) <= known
    for name in records.DECISION_EVENTS:
        assert transitions.Writer.DISPATCHER in transitions.task_transition(name).writers, name
    assert records.DECISION_EVENT_ORDER < "#10"                       # before run_task's TASK_STARTED
