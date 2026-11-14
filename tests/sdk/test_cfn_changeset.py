"""Tests for polyris/cfn_changeset.py — boto3 change-set deploys shared by polyris-deploy and polyris-up.

CloudFormation is a small fake client (the boto3 boundary): moto can't run
change sets for these templates, and the fake lets each test pin the exact
request sent.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

import pytest
from botocore.exceptions import ClientError, WaiterError

from polyris import cfn_changeset as C


class FakeCfn:
    def __init__(self, status: Optional[str] = None, change_states: Optional[List[Dict[str, Any]]] = None,
                 waiter_error: bool = False):
        self.status = status  # None = stack doesn't exist
        self.change_states = change_states or [{"Status": "CREATE_COMPLETE"}]
        self.waiter_error = waiter_error
        self.calls: List[tuple] = []

    def describe_stacks(self, StackName):
        if self.status is None:
            raise ClientError({"Error": {"Code": "ValidationError",
                                         "Message": f"Stack with id {StackName} does not exist"}}, "DescribeStacks")
        return {"Stacks": [{"StackName": StackName, "StackStatus": self.status}]}

    def create_change_set(self, **kw):
        self.calls.append(("create_change_set", kw))

    def describe_change_set(self, StackName, ChangeSetName):
        state = self.change_states.pop(0) if len(self.change_states) > 1 else self.change_states[0]
        return dict(state)

    def delete_change_set(self, **kw):
        self.calls.append(("delete_change_set", kw))

    def execute_change_set(self, **kw):
        self.calls.append(("execute_change_set", kw))

    def get_waiter(self, name):
        fake = self

        class _W:
            def wait(self, **kw):
                fake.calls.append(("wait", name))
                if fake.waiter_error:
                    raise WaiterError(name=name, reason="Waiter encountered a terminal failure state",
                                      last_response={})
        return _W()

    def describe_stack_events(self, StackName):
        return {"StackEvents": [{"EventId": "old"}]}

    def call(self, name: str) -> Dict[str, Any]:
        return next(kw for n, kw in self.calls if n == name)


def _create(cfn, **kw):
    kw.setdefault("stack_name", "s")
    kw.setdefault("capabilities", ["CAPABILITY_NAMED_IAM"])
    if "template_url" not in kw:
        kw.setdefault("template_body", "{}")
    return C.create_change_set(cfn, sleep=lambda _s: None, **kw)


# ── stack_status ─────────────────────────────────────────────────────────────


def test_stack_status():
    assert C.stack_status(FakeCfn(None), "s") is None
    assert C.stack_status(FakeCfn("UPDATE_COMPLETE"), "s") == "UPDATE_COMPLETE"

    class _Empty:
        def describe_stacks(self, StackName):
            return {"Stacks": []}
    assert C.stack_status(_Empty(), "s") is None


def test_stack_status_reraises_other_errors():
    class _Denied:
        def describe_stacks(self, StackName):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, "DescribeStacks")
    with pytest.raises(ClientError):
        C.stack_status(_Denied(), "s")


# ── create_change_set ────────────────────────────────────────────────────────


@pytest.mark.parametrize("status,expected", [
    (None, "CREATE"), ("REVIEW_IN_PROGRESS", "CREATE"), ("UPDATE_COMPLETE", "UPDATE"), ("", "UPDATE"),
])
def test_change_set_type_follows_stack_state(status, expected):
    cfn = FakeCfn(status)
    cs = _create(cfn)
    assert cfn.call("create_change_set")["ChangeSetType"] == expected
    assert cs["ChangeSetType"] == expected and cs["ChangeSetName"].startswith("polyris-")


def test_request_shape_with_body_url_and_tags():
    cfn = FakeCfn()
    _create(cfn, template_url="https://b.s3/t.json", parameters=[{"ParameterKey": "A", "ParameterValue": "1"}],
            tags=[{"Key": "k", "Value": "v"}], capabilities=("CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND"))
    kw = cfn.call("create_change_set")
    assert kw["TemplateURL"] == "https://b.s3/t.json" and "TemplateBody" not in kw
    assert kw["Tags"] == [{"Key": "k", "Value": "v"}]
    assert kw["Capabilities"] == ["CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND"]
    assert kw["Parameters"] == [{"ParameterKey": "A", "ParameterValue": "1"}]

    cfn = FakeCfn()
    _create(cfn)
    kw = cfn.call("create_change_set")
    assert kw["TemplateBody"] == "{}" and "Tags" not in kw


def test_exactly_one_template_source():
    with pytest.raises(ValueError):
        C.create_change_set(FakeCfn(), stack_name="s", capabilities=[])
    with pytest.raises(ValueError):
        C.create_change_set(FakeCfn(), stack_name="s", capabilities=[], template_body="{}", template_url="u")


def test_waits_until_ready():
    cfn = FakeCfn(change_states=[{"Status": "CREATE_PENDING"}, {"Status": "CREATE_IN_PROGRESS"},
                                 {"Status": "CREATE_COMPLETE", "ChangeSetName": "given"}])
    assert _create(cfn)["ChangeSetName"] == "given"


def test_no_changes_returns_none_and_deletes_change_set():
    cfn = FakeCfn("UPDATE_COMPLETE", change_states=[
        {"Status": "FAILED", "StatusReason": "The submitted information didn't contain changes."}])
    assert _create(cfn) is None
    assert cfn.call("delete_change_set")["StackName"] == "s"


def test_failed_change_set_raises_with_reason():
    cfn = FakeCfn(change_states=[{"Status": "FAILED", "StatusReason": "Template format error"}])
    with pytest.raises(C.ChangeSetError, match="Template format error"):
        _create(cfn)


def test_change_set_that_never_settles_times_out(monkeypatch):
    monkeypatch.setattr(C, "_CHANGE_SET_MAX_WAIT_S", 6)
    cfn = FakeCfn(change_states=[{"Status": "CREATE_PENDING"}])
    with pytest.raises(C.ChangeSetError, match="still CREATE_PENDING"):
        _create(cfn)


@pytest.mark.parametrize("status,message", [
    ("ROLLBACK_COMPLETE", "ROLLBACK_COMPLETE"),
    ("UPDATE_IN_PROGRESS", "busy"),
])
def test_unusable_stack_states(status, message):
    with pytest.raises(C.ChangeSetError, match=message):
        _create(FakeCfn(status))


# ── execute_change_set ───────────────────────────────────────────────────────


@pytest.mark.parametrize("cs_type,waiter", [("CREATE", "stack_create_complete"), ("UPDATE", "stack_update_complete")])
def test_execute_waits_with_matching_waiter(cs_type, waiter):
    cfn = FakeCfn()
    C.execute_change_set(cfn, stack_name="s", change_set={"ChangeSetName": "cs", "ChangeSetType": cs_type})
    assert cfn.calls == [("execute_change_set", {"StackName": "s", "ChangeSetName": "cs"}), ("wait", waiter)]


def test_execute_can_keep_a_failed_update_instead_of_rolling_back():
    cfn = FakeCfn()
    C.execute_change_set(cfn, stack_name="s", change_set={"ChangeSetName": "cs", "ChangeSetType": "UPDATE"},
                         disable_rollback=True)
    assert cfn.calls[0] == ("execute_change_set", {"StackName": "s", "ChangeSetName": "cs", "DisableRollback": True})


def test_execute_failure_raises():
    cfn = FakeCfn(waiter_error=True)
    with pytest.raises(C.ChangeSetError, match="did not finish"):
        C.execute_change_set(cfn, stack_name="s", change_set={"ChangeSetName": "cs", "ChangeSetType": "UPDATE"})


# ── start_event_watcher ──────────────────────────────────────────────────────


def test_event_watcher_starts_and_stops(mocker):
    seen_seed: list = []

    def fake_watch(cfn, stack_name, seen, stop, poll_interval=2.0):
        seen_seed.extend(seen)
        stop.wait()

    mocker.patch.object(C, "_watch_stack_events", side_effect=fake_watch)
    before = threading.active_count()
    stop = C.start_event_watcher(FakeCfn(), "s")
    stop()
    assert seen_seed == ["old"], "existing events are seeded so they aren't replayed"
    assert threading.active_count() == before


def test_event_watcher_tolerates_missing_stack(mocker):
    class _NoStack(FakeCfn):
        def describe_stack_events(self, StackName):
            raise ClientError({"Error": {"Code": "ValidationError", "Message": "does not exist"}}, "X")

    mocker.patch.object(C, "_watch_stack_events", side_effect=lambda cfn, n, seen, stop, **kw: stop.wait())
    C.start_event_watcher(_NoStack(), "s")()


def test_failed_change_set_is_deleted_and_cleanup_errors_dont_hide_the_cause(capsys):
    cfn = FakeCfn(change_states=[{"Status": "FAILED", "StatusReason": "Template format error"}])
    with pytest.raises(C.ChangeSetError, match="Template format error"):
        _create(cfn)
    assert cfn.call("delete_change_set")["StackName"] == "s"

    class _NoDelete(FakeCfn):
        def delete_change_set(self, **kw):
            raise ClientError({"Error": {"Code": "Throttling", "Message": "slow down"}}, "DeleteChangeSet")

    with pytest.raises(C.ChangeSetError, match="Template format error"):
        _create(_NoDelete(change_states=[{"Status": "FAILED", "StatusReason": "Template format error"}]))
    assert "could not delete failed change set" in capsys.readouterr().out


def test_event_watcher_prints_final_events_on_stop(mocker, capsys):
    """The stack can settle between two polls; stop() polls once more so the
    narration ends on the final COMPLETE line, not on IN_PROGRESS."""
    from datetime import datetime, timezone

    class _Settles(FakeCfn):
        calls_made = 0

        def describe_stack_events(self, StackName):
            self.calls_made += 1
            if self.calls_made == 1:  # seed: history before this deploy
                return {"StackEvents": [{"EventId": "old"}]}
            return {"StackEvents": [{"EventId": "old"}, {
                "EventId": "done", "Timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "ResourceStatus": "CREATE_COMPLETE", "ResourceType": "AWS::CloudFormation::Stack",
                "LogicalResourceId": "s"}]}

    mocker.patch.object(C, "_watch_stack_events", side_effect=lambda cfn, n, seen, stop, **kw: stop.wait())
    C.start_event_watcher(_Settles(), "s")()
    out = capsys.readouterr().out
    assert "CREATE_COMPLETE" in out and "old" not in out


def test_final_poll_tolerates_a_deleted_stack(mocker, capsys):
    class _Gone(FakeCfn):
        seeded = False

        def describe_stack_events(self, StackName):
            if not self.seeded:
                self.seeded = True
                return {"StackEvents": []}
            raise ClientError({"Error": {"Code": "ValidationError", "Message": "Stack s does not exist"}}, "X")

    mocker.patch.object(C, "_watch_stack_events", side_effect=lambda cfn, n, seen, stop, **kw: stop.wait())
    C.start_event_watcher(_Gone(), "s")()
    assert capsys.readouterr().out == ""


def test_change_set_changes_are_read_across_pages():
    """The data-loss guard must see a replacement listed on a later page."""
    class _Paged(FakeCfn):
        def describe_change_set(self, StackName, ChangeSetName, NextToken=None):
            if NextToken is None:
                return {"Status": "CREATE_COMPLETE", "Changes": [{"n": 1}], "NextToken": "p2"}
            if NextToken == "p2":
                return {"Changes": [{"n": 2}], "NextToken": "p3"}
            return {"Changes": [{"n": 3}]}

    cs = _create(_Paged())
    assert cs["Changes"] == [{"n": 1}, {"n": 2}, {"n": 3}] and "NextToken" not in cs
