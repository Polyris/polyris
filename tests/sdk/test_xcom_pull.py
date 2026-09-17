"""Tests for the xcom.pull() runtime helper (100% coverage)."""
import json
from datetime import datetime

import pytest

from polyris.xcom import (
    ENV_DATE,
    ENV_PIPELINE,
    ENV_RUN_ID,
    ENV_TABLE,
    ENV_TASK_NAME,
    PullError,
    XComError,
    XComManuallyResolvedError,
    XComMissingError,
    XComTruncatedError,
    XComUpstreamFailedError,
    _PUSH_MARKER_FIELD,
    _is_manual_marker,
    _resolve,
    _resolve_s3_pointer,
    get,
    pull,
    push,
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


def test_xcom_manually_resolved_error_subclasses_xcom_error():
    assert issubclass(XComManuallyResolvedError, XComError)


# ── Manual-resolution marker detection (§Fix #6) ───────────────────────────


def _manual_marker(resolution: str = "mark_success", operator: str = "alice@example.com",
                   reason: str = "verified via S3", pipeline_execution: str = "run-abc123"):
    return {
        "_manually_resolved": True,
        "_resolution": resolution,
        "_reason": reason,
        "_operator": operator,
        "_pipeline_execution": pipeline_execution,
    }


def test_is_manual_marker_true_for_full_marker():
    assert _is_manual_marker(_manual_marker()) is True


def test_is_manual_marker_false_for_non_marker_shapes():
    assert _is_manual_marker({"rows": 42}) is False
    assert _is_manual_marker({"_manually_resolved": False}) is False
    assert _is_manual_marker({"_manually_resolved": "yes"}) is False  # not literal True
    assert _is_manual_marker([1, 2, 3]) is False
    assert _is_manual_marker(None) is False
    assert _is_manual_marker(42) is False


def test_get_raises_manually_resolved_on_marker_in_event_inject():
    """The primary footgun: mark_success writes status='success' + marker.
    Without the marker check the caller silently receives the marker dict."""
    event = {
        "upstream": {
            "transform": {"status": "success", "output": _manual_marker()}
        }
    }
    with pytest.raises(XComManuallyResolvedError) as exc:
        get(event, "transform")
    assert exc.value.task_name == "transform"
    assert exc.value.resolution == "mark_success"
    assert exc.value.operator == "alice@example.com"
    assert exc.value.reason == "verified via S3"
    # `_pipeline_execution` surfaces in the message so a downstream operator
    # can spot cross-run bleed (the marker's run is named explicitly).
    assert exc.value.pipeline_execution == "run-abc123"
    assert "pipeline_execution: run-abc123" in str(exc.value)
    assert "manually resolved" in str(exc.value)


def test_get_manual_marker_error_precedes_status_check():
    """A manually-skipped upstream carries status='skip' AND a marker.
    Marker check runs first so caller gets the more informative error."""
    event = {
        "upstream": {
            "transform": {"status": "skip", "output": _manual_marker(resolution="skip")}
        }
    }
    with pytest.raises(XComManuallyResolvedError) as exc:
        get(event, "transform")
    assert exc.value.resolution == "skip"


def test_get_returns_marker_when_raise_on_manual_false():
    """Opt-out returns the marker dict verbatim for callers that want to
    introspect the operator's reason / route on the resolution."""
    marker = _manual_marker(resolution="mark_success", reason="paid outside pipeline")
    event = {"upstream": {"t": {"status": "success", "output": marker}}}
    result = get(event, "t", raise_on_manual=False)
    assert result == marker


def test_get_falls_back_to_unknown_operator_when_field_missing():
    """Records written before 1.0.0 carry no _operator — falls back to
    the same string the backend uses for auth-off routes ('unknown'), so
    SDK / UI / backend present one distinct label for 'no identity captured'
    rather than two ('operator' vs 'unknown') users have to learn."""
    legacy = {
        "_manually_resolved": True,
        "_resolution": "mark_success",
        "_reason": "old row",
    }
    event = {"upstream": {"t": {"status": "success", "output": legacy}}}
    with pytest.raises(XComManuallyResolvedError) as exc:
        get(event, "t")
    assert exc.value.operator == "unknown"
    assert "operator: unknown" in str(exc.value)


def test_pull_raises_manually_resolved_when_ddb_row_is_a_marker():
    """SDK is symmetric: the DDB path also catches the marker so Glue / ECS
    / Batch tasks (which have no event to pass) are protected identically."""
    ddb = FakeDDB({"result": {"S": json.dumps(_manual_marker())}})
    with pytest.raises(XComManuallyResolvedError):
        pull("t", pipeline="p", date="d", table="tbl", ddb_client=ddb)


def test_pull_returns_marker_when_raise_on_manual_false():
    ddb = FakeDDB({"result": {"S": json.dumps(_manual_marker())}})
    result = pull("t", pipeline="p", date="d", table="tbl",
                  ddb_client=ddb, raise_on_manual=False)
    assert result["_manually_resolved"] is True


def test_get_ddb_fallback_propagates_raise_on_manual(monkeypatch):
    """Undeclared-dep path (no event.upstream[task]) goes via DDB — the
    marker check must fire there too, not only on the event-inject path."""
    monkeypatch.setenv(ENV_PIPELINE, "p")
    monkeypatch.setenv(ENV_DATE, "d")
    monkeypatch.setenv(ENV_TABLE, "tbl")
    ddb = FakeDDB({"result": {"S": json.dumps(_manual_marker())}})
    with pytest.raises(XComManuallyResolvedError):
        get(None, "t", ddb_client=ddb)


def test_get_ddb_fallback_returns_marker_when_opted_out(monkeypatch):
    monkeypatch.setenv(ENV_PIPELINE, "p")
    monkeypatch.setenv(ENV_DATE, "d")
    monkeypatch.setenv(ENV_TABLE, "tbl")
    ddb = FakeDDB({"result": {"S": json.dumps(_manual_marker())}})
    result = get(None, "t", ddb_client=ddb, raise_on_manual=False)
    assert result["_manually_resolved"] is True


def test_get_organic_dict_output_that_happens_to_have_stray_field_is_not_a_marker():
    """A user-returned dict that just happens to include the string
    '_manually_resolved' (typo, coincidence, custom domain) is NOT treated
    as a marker — the field must be literally True."""
    weird = {"_manually_resolved": "no thanks", "rows": 42}
    event = {"upstream": {"t": {"status": "success", "output": weird}}}
    assert get(event, "t") == weird


def test_get_all_done_manual_skip_needs_both_opt_outs_to_read_marker():
    """The realistic ``trigger_rule='all_done'`` + manually-skipped upstream
    case. The caller must opt out of BOTH ``raise_on_failure`` (because the
    task's status is 'skip' — the action_name) AND ``raise_on_manual``
    (because the marker check runs first and would still fire). Pinning the
    combination here so a future ordering change is caught."""
    marker = _manual_marker(resolution="skip", operator="alice", reason="no data today")
    event = {"upstream": {"t": {"status": "skip", "output": marker}}}
    # Default: raises XComManuallyResolvedError (marker check runs before status check).
    with pytest.raises(XComManuallyResolvedError):
        get(event, "t")
    # Opt out of manual only: still raises status error.
    with pytest.raises(XComUpstreamFailedError):
        get(event, "t", raise_on_manual=False)
    # Opt out of failure only: manual check still fires.
    with pytest.raises(XComManuallyResolvedError):
        get(event, "t", raise_on_failure=False)
    # Both opt-outs: caller receives the marker dict verbatim for introspection.
    result = get(event, "t", raise_on_failure=False, raise_on_manual=False)
    assert result == marker


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


def test_get_event_truncated_and_ddb_missing_raises_truncated():
    """Event says _truncated but DDB row is completely absent → we still know it was
    truncated (event marker had the size), so raise XComTruncatedError with the size,
    not XComMissingError. The event's marker is authoritative — DDB just failed to
    salvage the full value. Regression gate for the `except XComMissingError → raise
    XComTruncatedError` branch in xcom.get()."""
    event = _event_with_upstream(gigantic={
        "output": {"_truncated": True, "_size": 42000},
        "status": "success",
    })
    ddb = FakeDDB(None)  # no DDB item at all (dep row got TTL-expired or never written)
    with pytest.raises(XComTruncatedError, match="gigantic") as exc:
        get(event, "gigantic", ddb_client=ddb)
    # Size from the event marker survives into the raised error.
    assert exc.value.size_bytes == 42000


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


# ── xcom.push() — writer for service tasks (§2.3) ──────────────────────


class FakePushDDB:
    """DDB stub capturing update_item calls for assertion."""
    def __init__(self):
        self.calls = []

    def update_item(self, **kwargs):
        self.calls.append(kwargs)
        return {}


def _set_push_env(monkeypatch, task="my-task", run_id="arn:aws:states:us-east-1:1:execution:w:r1"):
    """Set the four env vars xcom.push() reads via _resolve."""
    monkeypatch.setenv(ENV_PIPELINE, "sales")
    monkeypatch.setenv(ENV_DATE, "2026-07-07")
    monkeypatch.setenv(ENV_TABLE, "tokens")
    monkeypatch.setenv(ENV_TASK_NAME, task)
    monkeypatch.setenv(ENV_RUN_ID, run_id)
    # Ensure Lambda warning does NOT fire in default test env
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)


def test_push_writes_updateitem_to_output_key(monkeypatch):
    _set_push_env(monkeypatch)
    ddb = FakePushDDB()
    push({"rows": 100}, ddb_client=ddb)
    assert len(ddb.calls) == 1
    call = ddb.calls[0]
    assert call["TableName"] == "tokens"
    assert call["Key"]["execution_name"]["S"] == "output#sales#my-task#2026-07-07"


def test_push_sets_pushed_by_task_marker(monkeypatch):
    _set_push_env(monkeypatch)
    ddb = FakePushDDB()
    push({"v": 1}, ddb_client=ddb)
    call = ddb.calls[0]
    # Marker written as {"BOOL": True}
    assert call["ExpressionAttributeValues"][":p"] == {"BOOL": True}
    # ExpressionAttributeNames uses the SDK constant so §2.8 parity holds
    assert call["ExpressionAttributeNames"]["#p"] == _PUSH_MARKER_FIELD
    # UpdateExpression sets it
    assert "#p = :p" in call["UpdateExpression"]


def test_push_writes_pushed_run_id_for_stale_marker_rejection(monkeypatch):
    """B1 fix: run_id lets Check_Task_Pushed reject stale markers from prior runs."""
    _set_push_env(monkeypatch, run_id="arn:...:execution:w:current-run")
    ddb = FakePushDDB()
    push({"v": 1}, ddb_client=ddb)
    assert ddb.calls[0]["ExpressionAttributeValues"][":rid"]["S"] == "arn:...:execution:w:current-run"
    assert "pushed_run_id = :rid" in ddb.calls[0]["UpdateExpression"]


def test_push_increments_push_count(monkeypatch):
    """push_count uses DDB ADD — atomic increment, useful for debug."""
    _set_push_env(monkeypatch)
    ddb = FakePushDDB()
    push({"v": 1}, ddb_client=ddb)
    assert ddb.calls[0]["ExpressionAttributeValues"][":one"] == {"N": "1"}
    assert "ADD push_count :one" in ddb.calls[0]["UpdateExpression"]


def test_push_serializes_dict(monkeypatch):
    _set_push_env(monkeypatch)
    ddb = FakePushDDB()
    push({"nested": {"a": [1, 2]}}, ddb_client=ddb)
    result_field = ddb.calls[0]["ExpressionAttributeValues"][":r"]["S"]
    assert json.loads(result_field) == {"nested": {"a": [1, 2]}}


def test_push_serializes_primitive(monkeypatch):
    _set_push_env(monkeypatch)
    ddb = FakePushDDB()
    push(42, ddb_client=ddb)
    assert ddb.calls[0]["ExpressionAttributeValues"][":r"]["S"] == "42"


def test_push_serializes_none(monkeypatch):
    _set_push_env(monkeypatch)
    ddb = FakePushDDB()
    push(None, ddb_client=ddb)
    assert ddb.calls[0]["ExpressionAttributeValues"][":r"]["S"] == "null"


def test_push_serializes_list(monkeypatch):
    _set_push_env(monkeypatch)
    ddb = FakePushDDB()
    push([1, 2, 3], ddb_client=ddb)
    assert ddb.calls[0]["ExpressionAttributeValues"][":r"]["S"] == "[1, 2, 3]"


def test_push_non_serializable_raises_xcom_error(monkeypatch):
    """L1 fix: datetime is not JSON-serializable → clear XComError, not TypeError."""
    _set_push_env(monkeypatch)
    ddb = FakePushDDB()
    with pytest.raises(XComError, match="JSON-serializable"):
        push(datetime(2026, 1, 1), ddb_client=ddb)
    # Nothing hit DDB
    assert ddb.calls == []


def test_push_too_large_raises_with_s3_hint(monkeypatch):
    _set_push_env(monkeypatch)
    ddb = FakePushDDB()
    # 400_000 chars of JSON > 350_000 cap
    huge = "x" * 400_000
    with pytest.raises(XComError, match="_s3_ref"):
        push(huge, ddb_client=ddb)
    assert ddb.calls == []


def test_push_missing_task_name_env_gives_actionable_error(monkeypatch):
    """§2.3: 'run sam deploy' hint when POLYRIS_TASK_NAME missing (old wrapper)."""
    monkeypatch.setenv(ENV_PIPELINE, "p")
    monkeypatch.setenv(ENV_DATE, "d")
    monkeypatch.setenv(ENV_TABLE, "t")
    monkeypatch.delenv(ENV_TASK_NAME, raising=False)
    monkeypatch.delenv(ENV_RUN_ID, raising=False)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    ddb = FakePushDDB()
    with pytest.raises(XComError, match=ENV_TASK_NAME):
        push({"v": 1}, ddb_client=ddb)


def test_push_missing_run_id_env_gives_actionable_error(monkeypatch):
    """Same actionable hint for POLYRIS_WRAPPER_RUN_ID (B1 stale-marker guard)."""
    monkeypatch.setenv(ENV_PIPELINE, "p")
    monkeypatch.setenv(ENV_DATE, "d")
    monkeypatch.setenv(ENV_TABLE, "t")
    monkeypatch.setenv(ENV_TASK_NAME, "task")
    monkeypatch.delenv(ENV_RUN_ID, raising=False)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    ddb = FakePushDDB()
    with pytest.raises(XComError, match=ENV_RUN_ID):
        push({"v": 1}, ddb_client=ddb)


def test_push_from_lambda_emits_userwarning(monkeypatch):
    """B2: warn when called from Lambda runtime — race with wrapper Save_Success."""
    _set_push_env(monkeypatch)
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "my-lambda")
    ddb = FakePushDDB()
    with pytest.warns(UserWarning, match="Lambda"):
        push({"v": 1}, ddb_client=ddb)


def test_push_not_from_lambda_no_warning(monkeypatch):
    """No warning for Glue/ECS/Batch/EMR — those are the intended callers."""
    _set_push_env(monkeypatch)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    ddb = FakePushDDB()
    # pytest.warns would fail if no warning caught; use warnings.catch_warnings + assert 0
    import warnings as _w
    with _w.catch_warnings(record=True) as caught:
        _w.simplefilter("always")
        push({"v": 1}, ddb_client=ddb)
        assert [w for w in caught if issubclass(w.category, UserWarning)] == []


def test_push_explicit_kwargs_bypass_env(monkeypatch):
    """Local tests / cross-account workarounds: pass context as kwargs."""
    monkeypatch.delenv(ENV_PIPELINE, raising=False)
    monkeypatch.delenv(ENV_TASK_NAME, raising=False)
    monkeypatch.delenv(ENV_RUN_ID, raising=False)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    ddb = FakePushDDB()
    push({"v": 1}, pipeline="p", task="t", date="d", table="tbl",
         run_id="test-run", ddb_client=ddb)
    assert ddb.calls[0]["Key"]["execution_name"]["S"] == "output#p#t#d"
