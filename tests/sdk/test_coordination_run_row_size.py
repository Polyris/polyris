"""The largest run row the code can write stays under SIZE_MAX (ADR #137 §3, D114).

Every decision's transaction checks the run row, and DynamoDB bills the whole
item, so the row has a bound: its fields' caps and the builders that apply
them (run_row.py), at most ten pause intervals (pauses.py), and a summary of
one count per status. The size follows DynamoDB's documented rules:
attribute names and UTF-8 strings by their bytes, a number by its digits,
a list or map with 3 bytes of overhead and 1 byte per element.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from polyris.constants import TaskStatus
from polyris.coordination import attention, pauses, run_end, run_row, task_spec

T0 = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)
ISO = "2026-11-18T06:00:00.000Z"


def size(value):
    if isinstance(value, bool) or value is None:
        return 1
    if isinstance(value, (int, Decimal)):
        digits = len(str(abs(int(value))).rstrip("0")) or 1
        return (digits + 1) // 2 + 1
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, dict):
        return 3 + sum(len(k.encode("utf-8")) + size(v) + 1 for k, v in value.items())
    if isinstance(value, list):
        return 3 + sum(size(v) + 1 for v in value)
    raise TypeError(type(value))


def item_size(item):
    return sum(len(name.encode("utf-8")) + size(value) for name, value in item.items())


def largest_run_row():
    long_text = "Зупинено: " + "ї" * 2000           # two bytes a character, clipped by bytes
    who = "оператор-" + "щ" * 400
    pipeline_execution = "p" * 56 + "-run-2026-11-18-abcd1234"
    assert len(pipeline_execution) == 80              # Step Functions' longest execution name
    intervals = ()
    for k in range(pauses.MAX_INTERVALS + 3):
        intervals = pauses.pause(intervals, T0 + timedelta(hours=k))
        if k < pauses.MAX_INTERVALS + 2:
            intervals = pauses.resume(intervals, T0 + timedelta(hours=k, minutes=30))
    order = run_row.token_order(redrive_count=99_999, retry_count=99_999_999)
    return {
        run_row.TABLE_KEY: run_row.key(pipeline_execution),
        run_row.STATUS: run_row.CREATING,
        run_row.TOKEN: "t" * run_row.TOKEN_MAX,
        run_row.TOKEN_ORDER: order,
        run_row.ANSWERED: order,
        run_row.STOP: run_row.stop_mark(by=who, reason=long_text, at=ISO),
        run_row.STOP_DONE: ISO,
        run_row.RUN_VERSION: 99_999_999,
        run_row.LEFT_AT: ISO,
        run_row.EXECUTION: pipeline_execution,
        run_row.RUN_START: "2026-11-18T06:00:00.123456+00:00",
        run_row.PIPELINE: "p" * 56,
        run_row.RUN_DATE: "2026-11-18",
        run_row.START_DAY: "2026-11-18",
        run_row.STARTED_AT: ISO,
        run_row.FINISHED_AT: ISO,
        run_row.TRIGGERED_BY: run_row.clip("backfill:" + "b" * 400, run_row.TRIGGERED_BY_MAX),
        run_row.SPEC_ID: task_spec.spec_id("p" * 56, "f" * 64),
        run_row.TASK_COUNT: 2000,
        run_row.FORMAT: task_spec.FORMAT_VERSION,
        run_row.SUMMARY: {s.value: 2000 for s in TaskStatus},
        run_end.SUMMARY_AT: ISO,
        run_row.STEP_FAILURE: run_row.step_mark(step="s" * 300, error="States." + "e" * 300),
        run_row.REQUESTS: {
            run_row.RENEW_TTL: run_row.request(by=who, at=ISO),
            run_row.RESET_DEPENDANTS: run_row.request(by=who, at=ISO, task="t" * run_row.TASK_ID_MAX),
        },
        run_row.PAUSES: pauses.to_item(intervals),
        run_row.TTL: 1_830_000_000,
        attention.KEY: attention.attention(attention.WAITING, run_row.key(pipeline_execution)),
        attention.DUE: ISO,
    }


def test_the_largest_row_has_every_field_of_the_run_row():
    assert set(run_row.FIELDS) <= set(largest_run_row())


def test_the_largest_run_row_stays_under_its_bound():
    row = largest_run_row()
    assert len(row[run_row.PAUSES]) == pauses.MAX_INTERVALS
    assert item_size(row) <= run_row.SIZE_MAX, item_size(row)


def test_text_is_clipped_by_utf8_bytes_and_never_inside_a_character():
    clipped = run_row.clip("ї" * 400, 100)
    assert len(clipped.encode("utf-8")) <= 100 and clipped.endswith("…")
    assert run_row.clip("short", 100) == "short"
    assert run_row.stop_mark(by="ops", reason="bad input", at=ISO) == {"by": "ops", "reason": "bad input", "at": ISO}


def test_a_request_names_its_task_whole_or_refuses_it():
    assert run_row.request(by="ops", at=ISO, task="load") == {"by": "ops", "at": ISO, "task": "load"}
    assert run_row.request(by="ops", at=ISO) == {"by": "ops", "at": ISO}
    try:
        run_row.request(by="ops", at=ISO, task="t" * (run_row.TASK_ID_MAX + 1))
    except ValueError as exc:
        assert "cannot be named in a request" in str(exc)
    else:
        raise AssertionError("a task id past the cap must be refused, not clipped")
