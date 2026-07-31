"""Tests for GET /api/task-output (routes.tasks.get_task_output).

pytest-mock (ADR #26): patch resolve_task_item and the DynamoDB Table boundary;
the parse/branch logic runs for real (CLAUDE.md #13).
"""
import json

from routes.tasks import get_task_output


def _event(date="2026-07-07", pipeline_execution="p-run-2026-07-07-abc"):
    return {"queryStringParameters": {"date": date, "pipeline_execution": pipeline_execution}}


def _body(resp):
    assert resp["statusCode"] == 200
    return json.loads(resp["body"])


class _Table:
    def __init__(self, item):
        self._item = item
        self.calls = []

    def get_item(self, **kwargs):
        self.calls.append(kwargs)
        return {"Item": self._item} if self._item is not None else {}


def _patch(mocker, *, item=("pipeline_name", "sales"), store=None, retrieve=None,
           task_name="extract", date="2026-07-07"):
    task_item = {"pipeline_name": item[1], "task_name": task_name, "date": date} if item else {}
    mocker.patch("routes.tasks.resolve_task_item", return_value=(task_item, "extract-2026-07-07-abc"))
    table = _Table(store)
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
