"""Tests for the xcom.pull() runtime helper (100% coverage)."""
import json

import pytest

from polyris.xcom import (
    ENV_DATE,
    ENV_PIPELINE,
    ENV_TABLE,
    PullError,
    _resolve,
    _resolve_s3_pointer,
    pull,
)


class FakeDDB:
    """Minimal DynamoDB client double."""
    def __init__(self, item):
        self._item = item
        self.calls = []

    def get_item(self, **kwargs):
        self.calls.append(kwargs)
        return {"Item": self._item} if self._item is not None else {}


class FakeS3:
    def __init__(self, body_bytes):
        self._body = body_bytes
        self.calls = []

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        return {"Body": _FakeBody(self._body)}


class _FakeBody:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


def _item(result_str):
    return {"result": {"S": result_str}}


# ── pull: happy paths ────────────────────────────────────────────────
def test_pull_inline_output():
    ddb = FakeDDB(_item(json.dumps({"count": 42, "path": "s3://x"})))
    out = pull("extract", pipeline="p", date="2026-07-07", table="t", ddb_client=ddb)
    assert out == {"count": 42, "path": "s3://x"}
    # key is built correctly
    assert ddb.calls[0]["Key"] == {"execution_name": {"S": "output#p#extract#2026-07-07"}}
    assert ddb.calls[0]["TableName"] == "t"


def test_pull_reads_context_from_env(monkeypatch):
    monkeypatch.setenv(ENV_PIPELINE, "envpipe")
    monkeypatch.setenv(ENV_DATE, "2026-01-01")
    monkeypatch.setenv(ENV_TABLE, "envtable")
    ddb = FakeDDB(_item(json.dumps({"ok": True})))
    out = pull("t1", ddb_client=ddb)
    assert out == {"ok": True}
    assert ddb.calls[0]["Key"]["execution_name"]["S"] == "output#envpipe#t1#2026-01-01"


def test_pull_non_dict_output():
    ddb = FakeDDB(_item(json.dumps([1, 2, 3])))
    assert pull("t", pipeline="p", date="d", table="t", ddb_client=ddb) == [1, 2, 3]


def test_pull_resolves_s3_pointer():
    ddb = FakeDDB(_item(json.dumps({"_s3_ref": "s3://bucket/path/out.json"})))
    s3 = FakeS3(json.dumps({"big": "payload"}).encode())
    out = pull("t", pipeline="p", date="d", table="tbl", ddb_client=ddb, s3_client=s3)
    assert out == {"big": "payload"}
    assert s3.calls[0] == {"Bucket": "bucket", "Key": "path/out.json"}


# ── pull: error paths ────────────────────────────────────────────────
def test_pull_no_item_raises():
    with pytest.raises(PullError, match="no output stored"):
        pull("missing", pipeline="p", date="d", table="t", ddb_client=FakeDDB(None))


def test_pull_item_without_result_raises():
    with pytest.raises(PullError, match="no output stored"):
        pull("t", pipeline="p", date="d", table="t", ddb_client=FakeDDB({"status": {"S": "success"}}))


def test_pull_unreadable_json_raises():
    ddb = FakeDDB(_item("not-valid-json{"))
    with pytest.raises(PullError, match="not readable JSON"):
        pull("t", pipeline="p", date="d", table="t", ddb_client=ddb)


def test_pull_truncated_output_raises():
    ddb = FakeDDB(_item(json.dumps({"_truncated": True, "_size": 300000})))
    with pytest.raises(PullError, match="truncated"):
        pull("t", pipeline="p", date="d", table="t", ddb_client=ddb)


def test_pull_reads_context_from_lambda_event():
    """Lambda: context comes from the event (has pipeline_name + current_date)."""
    event = {"pipeline_name": "sales", "date": "2026-07-07", "_polyris_table": "tok"}
    ddb = FakeDDB(_item(json.dumps({"n": 1})))
    out = pull("extract", event, ddb_client=ddb)
    assert out == {"n": 1}
    assert ddb.calls[0]["Key"]["execution_name"]["S"] == "output#sales#extract#2026-07-07"
    assert ddb.calls[0]["TableName"] == "tok"


def test_pull_prefers_date_over_current_date():
    """The store keys on 'date'; pull must use it (not current_date) when both exist."""
    event = {"pipeline_name": "p", "date": "2026-07-07", "current_date": "2020-01-01", "_polyris_table": "t"}
    ddb = FakeDDB(_item(json.dumps({"ok": 1})))
    pull("x", event, ddb_client=ddb)
    assert ddb.calls[0]["Key"]["execution_name"]["S"] == "output#p#x#2026-07-07"


def test_pull_explicit_args_override_context_and_env(monkeypatch):
    monkeypatch.setenv(ENV_PIPELINE, "envpipe")
    event = {"pipeline_name": "ctxpipe", "current_date": "2020-01-01"}
    ddb = FakeDDB(_item(json.dumps({"x": 1})))
    pull("t", event, pipeline="explicit", date="2026-12-31", table="t2", ddb_client=ddb)
    assert ddb.calls[0]["Key"]["execution_name"]["S"] == "output#explicit#t#2026-12-31"


# ── _resolve ─────────────────────────────────────────────────────────
def test_resolve_explicit_value_wins():
    assert _resolve("given", {"pipeline_name": "ctx"}, ("pipeline_name",), "E", "thing") == "given"


def test_resolve_from_context():
    assert _resolve(None, {"date": "d1"}, ("current_date", "date"), ENV_DATE, "date") == "d1"


def test_resolve_from_env(monkeypatch):
    monkeypatch.setenv(ENV_TABLE, "envtbl")
    assert _resolve(None, {}, ("_polyris_table",), ENV_TABLE, "table") == "envtbl"


def test_resolve_missing_raises(monkeypatch):
    monkeypatch.delenv(ENV_TABLE, raising=False)
    with pytest.raises(PullError, match=ENV_TABLE):
        _resolve(None, {}, ("_polyris_table",), ENV_TABLE, "table name")


# ── _resolve_s3_pointer ──────────────────────────────────────────────
def test_resolve_s3_pointer_strips_scheme():
    s3 = FakeS3(json.dumps({"v": 1}).encode())
    assert _resolve_s3_pointer("s3://b/k/file.json", s3) == {"v": 1}
    assert s3.calls[0] == {"Bucket": "b", "Key": "k/file.json"}


def test_resolve_s3_pointer_without_scheme():
    s3 = FakeS3(json.dumps({"v": 2}).encode())
    assert _resolve_s3_pointer("b/k", s3) == {"v": 2}
    assert s3.calls[0] == {"Bucket": "b", "Key": "k"}
