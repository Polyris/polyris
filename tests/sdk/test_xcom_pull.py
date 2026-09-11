"""Tests for the xcom.pull() runtime helper (100% coverage)."""
import json

import pytest

from polyris.xcom import (
    ENV_DATE,
    ENV_PIPELINE,
    ENV_TABLE,
    PullError,
    XComError,
    XComMissingError,
    XComTruncatedError,
    XComUpstreamFailedError,
    _resolve,
    _resolve_s3_pointer,
    get,
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


# ── XComError hierarchy + PullError alias (§2.1 + backward compat) ─────


def test_xcom_error_is_runtime_error():
    """Base XComError inherits RuntimeError so `except RuntimeError:` still catches."""
    assert issubclass(XComError, RuntimeError)


def test_xcom_missing_error_subclasses_xcom_error():
    assert issubclass(XComMissingError, XComError)


def test_xcom_upstream_failed_error_subclasses_xcom_error():
    assert issubclass(XComUpstreamFailedError, XComError)


def test_xcom_truncated_error_subclasses_xcom_error():
    assert issubclass(XComTruncatedError, XComError)


def test_pull_error_is_alias_for_xcom_missing_error():
    """Backward-compat: old `except PullError` catches new XComMissingError raises."""
    assert PullError is XComMissingError


def test_pull_error_still_raised_by_pull_no_item():
    """Regression: existing pull() code raising PullError works via alias."""
    with pytest.raises(PullError, match="no output stored"):
        pull("missing", pipeline="p", date="d", table="t", ddb_client=FakeDDB(None))


def test_pull_error_also_caught_as_xcom_missing_error():
    """Same raise, caught via new class name — proves alias works both directions."""
    with pytest.raises(XComMissingError, match="no output stored"):
        pull("missing", pipeline="p", date="d", table="t", ddb_client=FakeDDB(None))


def test_xcom_missing_error_message_includes_context_when_provided():
    err = XComMissingError("extract", pipeline="sales", date="2026-01-01")
    msg = str(err)
    assert "extract" in msg
    assert "sales" in msg
    assert "2026-01-01" in msg


def test_xcom_missing_error_message_without_context():
    err = XComMissingError("extract")
    msg = str(err)
    assert "extract" in msg
    assert "did the task run" in msg


def test_xcom_upstream_failed_error_message_mentions_raise_on_failure():
    err = XComUpstreamFailedError("extract", "failed")
    msg = str(err)
    assert "extract" in msg
    assert "failed" in msg
    assert "raise_on_failure=False" in msg


def test_xcom_truncated_error_message_mentions_s3_claim_check():
    err = XComTruncatedError("extract", size_bytes=400000)
    msg = str(err)
    assert "extract" in msg
    assert "400000" in msg
    assert "_s3_ref" in msg
    assert "S3" in msg or "Claim Check" in msg


# ── xcom.get() — uniform reader (§2.2) ─────────────────────────────────


def _event_with_upstream(**deps):
    """Build a minimal Lambda event with the given upstream deps.

    Each kwarg is a dep name; value is the {output, status} entry dict.
    """
    return {
        "pipeline_name": "p",
        "date": "2026-07-07",
        "_polyris_table": "t",
        "upstream": deps,
    }


def test_get_from_event_upstream_dict():
    event = _event_with_upstream(extract={"output": {"rows": 100}, "status": "success"})
    assert get(event, "extract") == {"rows": 100}


def test_get_from_event_upstream_primitive():
    """Post-$isJson-fix: primitives flow through untouched."""
    event = _event_with_upstream(count={"output": 42, "status": "success"})
    assert get(event, "count") == 42


def test_get_from_event_upstream_none():
    event = _event_with_upstream(nullable={"output": None, "status": "success"})
    assert get(event, "nullable") is None


def test_get_from_event_upstream_list():
    event = _event_with_upstream(items={"output": [1, 2, 3], "status": "success"})
    assert get(event, "items") == [1, 2, 3]


def test_get_missing_status_unknown_raises():
    """Get_Dep_Output writes status=unknown when DDB row is absent."""
    event = _event_with_upstream(gone={"output": {}, "status": "unknown"})
    with pytest.raises(XComMissingError, match="gone"):
        get(event, "gone")


def test_get_missing_no_raise_returns_none():
    event = _event_with_upstream(gone={"output": {}, "status": "unknown"})
    assert get(event, "gone", raise_on_missing=False) is None


def test_get_failed_status_raises():
    event = _event_with_upstream(broken={"output": {}, "status": "failed"})
    with pytest.raises(XComUpstreamFailedError, match="broken"):
        get(event, "broken")


def test_get_failed_no_raise_returns_output():
    """all_done trigger opt-in: reader wants failed upstream's output too."""
    event = _event_with_upstream(broken={"output": {"partial": True}, "status": "failed"})
    assert get(event, "broken", raise_on_failure=False) == {"partial": True}


def test_get_success_with_empty_dict_returns_empty_dict():
    """status=success + output={} is a VALID user-returned empty dict (M5 fix).

    Only status=unknown means missing — empty dict alone must not trigger XComMissingError.
    """
    event = _event_with_upstream(empty={"output": {}, "status": "success"})
    assert get(event, "empty") == {}


def test_get_event_truncated_falls_back_to_pull():
    """Runtime inject cap is 25KB per dep; DDB result field allows 350KB.
    When event marker says truncated, get() reads DDB directly (may have full data).
    """
    event = _event_with_upstream(big={
        "output": {"_truncated": True, "_size": 30000},
        "status": "success",
    })
    # Provide a DDB client with the full non-truncated payload
    full = {"rows": list(range(100))}
    ddb = FakeDDB(_item(json.dumps(full)))
    assert get(event, "big", ddb_client=ddb) == full


def test_get_event_and_ddb_both_truncated_raises():
    """If DDB also has _truncated marker → XComTruncatedError with size from event."""
    event = _event_with_upstream(gigantic={
        "output": {"_truncated": True, "_size": 500000},
        "status": "success",
    })
    ddb = FakeDDB(_item(json.dumps({"_truncated": True, "_size": 500000})))
    with pytest.raises(XComTruncatedError, match="gigantic"):
        get(event, "gigantic", ddb_client=ddb)


def test_get_s3_ref_in_event_auto_resolves():
    event = _event_with_upstream(big={
        "output": {"_s3_ref": "s3://lake/out/big.json"},
        "status": "success",
    })
    s3 = FakeS3(json.dumps({"real": "payload"}).encode())
    assert get(event, "big", s3_client=s3) == {"real": "payload"}


def test_get_undeclared_dep_falls_back_to_pull():
    """Dep not in event.upstream → get() delegates to pull() (DDB read)."""
    event = _event_with_upstream(other={"output": {"x": 1}, "status": "success"})
    ddb = FakeDDB(_item(json.dumps({"y": 2})))
    assert get(event, "undeclared", ddb_client=ddb) == {"y": 2}


def test_get_no_event_uses_pull(monkeypatch):
    """Service tasks (Glue/ECS/Batch) pass event=None; get() uses env-based pull()."""
    monkeypatch.setenv(ENV_PIPELINE, "p")
    monkeypatch.setenv(ENV_DATE, "2026-07-07")
    monkeypatch.setenv(ENV_TABLE, "t")
    ddb = FakeDDB(_item(json.dumps({"ok": True})))
    result = get(None, "extract", ddb_client=ddb)
    assert result == {"ok": True}
    # Proves we went through pull() (which built the key from env + task_name)
    assert ddb.calls[0]["Key"]["execution_name"]["S"] == "output#p#extract#2026-07-07"


def test_get_no_event_no_context_returns_none_when_soft(monkeypatch):
    """Glue/ECS with missing env in soft mode returns None instead of raising."""
    # Ensure env is clean so _resolve raises
    monkeypatch.delenv(ENV_PIPELINE, raising=False)
    monkeypatch.delenv(ENV_DATE, raising=False)
    monkeypatch.delenv(ENV_TABLE, raising=False)
    assert get(None, "extract", raise_on_missing=False) is None


def test_get_no_event_no_context_raises_by_default(monkeypatch):
    """Loud default: missing context → clear error propagated up."""
    monkeypatch.delenv(ENV_PIPELINE, raising=False)
    monkeypatch.delenv(ENV_DATE, raising=False)
    monkeypatch.delenv(ENV_TABLE, raising=False)
    with pytest.raises(XComMissingError):
        get(None, "extract")


def test_get_missing_task_name_raises_value_error():
    with pytest.raises(ValueError, match="task_name"):
        get({}, None)


def test_get_malformed_upstream_entry_passed_through():
    """Legacy raw-value shape (non-dict entry) should not crash."""
    event = _event_with_upstream(raw="just a string, not a dict")
    assert get(event, "raw") == "just a string, not a dict"
