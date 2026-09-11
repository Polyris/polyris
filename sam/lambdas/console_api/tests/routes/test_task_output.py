"""Tests for GET /api/task-output (routes.tasks.get_task_output).

pytest-mock (ADR #26): patch resolve_task_item and the DynamoDB Table boundary;
the parse/branch logic runs for real (CLAUDE.md #13).
"""
import json

from botocore.exceptions import ClientError

from routes.tasks import get_task_output


def _event(date="2026-07-07", pipeline_execution="p-run-2026-07-07-abc"):
    return {"queryStringParameters": {"date": date, "pipeline_execution": pipeline_execution}}


def _body(resp):
    assert resp["statusCode"] == 200
    return json.loads(resp["body"])


class _MultiKeyTable:
    """Fake DDB table returning different items per execution_name key.

    Supports the 2-GetItem pattern get_task_output uses in 0.100.0+:
    one call for the output# row (result), one for the input# row (task_input).
    """

    def __init__(self, items_by_key=None, raise_on_key=None):
        self._items = items_by_key or {}
        # If set, get_item(Key.execution_name == raise_on_key) raises ClientError.
        # Used to test the fallback path when input# lookup fails.
        self._raise_on_key = raise_on_key
        self.calls = []

    def get_item(self, **kwargs):
        self.calls.append(kwargs)
        key = kwargs["Key"]["execution_name"]
        if self._raise_on_key is not None and key == self._raise_on_key:
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "simulated"}},
                "GetItem",
            )
        item = self._items.get(key)
        return {"Item": item} if item is not None else {}


def _patch(mocker, *, item=("pipeline_name", "sales"), store=None, input_store=None,
           retrieve=None, raise_on_input_key=False,
           task_name="extract", date="2026-07-07"):
    """Patch resolve_task_item + repo table for get_task_output tests.

    - store        → seeds the output#{pipeline}#{task}#{date} row (result, legacy task_input).
    - input_store  → seeds the input#{pipeline}#{task}#{date} row (new task_input home).
    - raise_on_input_key → simulate DDB failure on the input# lookup only,
                           to exercise the fallback to legacy task_input on output# row.
    """
    task_item = {"pipeline_name": item[1], "task_name": task_name, "date": date} if item else {}
    mocker.patch("routes.tasks.resolve_task_item", return_value=(task_item, "extract-2026-07-07-abc"))

    items_by_key = {}
    pipeline = item[1] if item else "sales"
    output_key = f"output#{pipeline}#{task_name}#{date}"
    input_key = f"input#{pipeline}#{task_name}#{date}"
    if store is not None:
        items_by_key[output_key] = store
    if input_store is not None:
        items_by_key[input_key] = input_store

    table = _MultiKeyTable(
        items_by_key,
        raise_on_key=input_key if raise_on_input_key else None,
    )
    # Patch the repo's table property, not a raw dynamodb.Table: the real
    # ExecutionsRepo.get() then runs against the fake, so the test still
    # exercises production code rather than a stand-in for it (#14).
    from dal.executions_repo import ExecutionsRepo
    mocker.patch.object(ExecutionsRepo, 'table', new_callable=mocker.PropertyMock,
                        return_value=table)
    if retrieve is not None:
        mocker.patch("routes.tasks.retrieve_result", side_effect=retrieve)
    return table


def test_returns_inline_output(mocker):
    table = _patch(mocker, store={"result": json.dumps({"rows": 1240})})
    body = _body(get_task_output("extract", _event()))
    assert body["output"] == {"rows": 1240}
    assert body["input"] is None            # no task_input stored
    assert body["truncated"] is False
    assert table.calls[0]["Key"] == {"execution_name": "output#sales#extract#2026-07-07"}


def test_returns_input_when_stored(mocker):
    stored_input = {"upstream": {"a": {"output": {"n": 1}}}, "variables": {"year": "2026"}}
    _patch(mocker, store={
        "result": json.dumps({"rows": 5}),
        "task_input": json.dumps(stored_input),
    })
    body = _body(get_task_output("extract", _event()))
    assert body["output"] == {"rows": 5}
    assert body["input"] == stored_input


def test_key_uses_resolved_plain_task_name_not_route_param(mocker):
    """Route param may be a full execution_name; the store key must use the plain
    task_name + date from the resolved item, not the raw param."""
    table = _patch(mocker, task_name="extract", date="2026-07-07",
                   store={"result": json.dumps({"ok": 1})})
    get_task_output("extract-2026-07-07-abc", _event())   # caller passes execution_name
    assert table.calls[0]["Key"] == {"execution_name": "output#sales#extract#2026-07-07"}


def test_missing_output_returns_null(mocker):
    _patch(mocker, store=None)
    body = _body(get_task_output("extract", _event()))
    assert body["output"] is None
    assert body["truncated"] is False


def test_truncated_output_flagged(mocker):
    _patch(mocker, store={"result": json.dumps({"_truncated": True, "_size": 400000})})
    body = _body(get_task_output("extract", _event()))
    assert body["output"] is None
    assert body["truncated"] is True


def test_s3_ref_resolved(mocker):
    _patch(mocker, store={"result": json.dumps({"_s3_ref": "s3://b/k.json"})},
           retrieve=lambda v: {"big": "payload"})
    body = _body(get_task_output("extract", _event()))
    assert body["output"] == {"big": "payload"}


def test_no_pipeline_name_returns_null(mocker):
    _patch(mocker, item=None, store={"result": json.dumps({"x": 1})})
    body = _body(get_task_output("extract", _event()))
    assert body["output"] is None


def test_result_read_error_is_swallowed(mocker):
    _patch(mocker, store={"result": "not-valid-json{"})
    body = _body(get_task_output("extract", _event()))
    assert body["output"] is None
    assert body["truncated"] is False


# ── Split records: prefer input# record, fall back to legacy field (§3.2) ────


def test_input_read_from_new_input_record_when_present(mocker):
    """0.100.0+ pipelines: task_input lives on the separate input# row.
    That's the primary source; the legacy field on output# is fallback only."""
    new_input = {"upstream": {"a": {"output": {"n": 1}, "status": "success"}},
                 "variables": {"year": "2026"}}
    table = _patch(
        mocker,
        store={"result": json.dumps({"rows": 5})},           # no task_input on output# row
        input_store={"task_input": json.dumps(new_input)},   # new-shape row
    )
    body = _body(get_task_output("extract", _event()))
    assert body["input"] == new_input
    assert body["output"] == {"rows": 5}
    # Two GetItem calls: output# first, input# second
    keys = [c["Key"]["execution_name"] for c in table.calls]
    assert keys == ["output#sales#extract#2026-07-07", "input#sales#extract#2026-07-07"]


def test_input_falls_back_to_legacy_task_input_on_output_row_when_new_record_absent(mocker):
    """Pre-0.100.0 pipelines: input# record doesn't exist yet, so we still
    display task_input from the output# row's legacy field."""
    legacy_input = {"upstream": {"legacy": {"output": {"k": 1}, "status": "success"}},
                    "variables": {}}
    _patch(
        mocker,
        store={"result": json.dumps({"rows": 5}), "task_input": json.dumps(legacy_input)},
        input_store=None,   # no new-shape row
    )
    body = _body(get_task_output("extract", _event()))
    assert body["input"] == legacy_input


def test_input_prefers_new_record_over_legacy_field_when_both_present(mocker):
    """Migration case: legacy output# row still has the pre-split task_input,
    AND the new wrapper wrote input# too. Prefer the new record — it's the
    current run's truth; legacy field may be from an older execution."""
    new_input = {"upstream": {}, "variables": {"tag": "new"}}
    legacy_input = {"upstream": {}, "variables": {"tag": "legacy-stale"}}
    _patch(
        mocker,
        store={"result": json.dumps({"ok": True}), "task_input": json.dumps(legacy_input)},
        input_store={"task_input": json.dumps(new_input)},
    )
    body = _body(get_task_output("extract", _event()))
    assert body["input"] == new_input


def test_neither_record_has_input_returns_null(mocker):
    """Task ran, wrote a result, but no task_input recorded anywhere (odd but
    possible with wrapper DDB failures)."""
    _patch(mocker, store={"result": json.dumps({"rows": 1})}, input_store=None)
    body = _body(get_task_output("extract", _event()))
    assert body["input"] is None
    assert body["output"] == {"rows": 1}


def test_input_record_read_error_falls_back_gracefully(mocker):
    """If DDB fails on the input# GetItem specifically, we must still fall back
    to the legacy task_input on the output# row — a transient input# failure
    can't hide input the legacy row can still surface."""
    legacy_input = {"upstream": {}, "variables": {"y": "fallback"}}
    _patch(
        mocker,
        store={"result": json.dumps({"rows": 1}), "task_input": json.dumps(legacy_input)},
        raise_on_input_key=True,
    )
    body = _body(get_task_output("extract", _event()))
    assert body["input"] == legacy_input
    assert body["output"] == {"rows": 1}


def test_input_read_never_calls_input_key_when_no_pipeline_name(mocker):
    """Guard: if resolve_task_item returns no pipeline_name, we short-circuit
    before any GetItem call — no wasted DDB read."""
    table = _patch(mocker, item=None, store={"result": json.dumps({"x": 1})})
    body = _body(get_task_output("extract", _event()))
    assert body["input"] is None
    assert body["output"] is None
    assert table.calls == []
