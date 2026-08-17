"""Tests for the BucketCleanup custom-resource Lambda.

Follows ADR #26: pytest-mock (`mocker`) throughout, boundary mocks only.
The Lambda's job is narrow: on a Delete event when the parent stack is
actually being torn down, empty every bucket named in
``ResourceProperties.Buckets``, then always signal SUCCESS back to
CloudFormation (a FAILED here would leave the stack stuck in
DELETE_FAILED forever). These tests pin exactly that contract, including
the data-loss guard: a Delete during a stack UPDATE (someone toggled
``AutoEmptyBucketsOnDelete=false`` and ran ``sam deploy``) must not
touch the buckets.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

# ``cfnresponse`` is provided by the AWS Lambda runtime, not by pip. Stub it
# before importing ``index`` so the test suite can load the module locally.
if "cfnresponse" not in sys.modules:
    _stub = types.ModuleType("cfnresponse")
    _stub.SUCCESS = "SUCCESS"
    _stub.FAILED = "FAILED"
    _stub.send = lambda *a, **kw: None
    sys.modules["cfnresponse"] = _stub

# Add this Lambda's dir to sys.path so `import index` works.
sys.path.insert(0, str(Path(__file__).parent))

import index  # noqa: E402


def _delete_event(buckets, stack_id="arn:aws:cloudformation:us-east-1:111:stack/x/y"):
    return {
        "RequestType": "Delete",
        "StackId": stack_id,
        "RequestId": "req-1",
        "LogicalResourceId": "BucketCleanup",
        "ResourceType": "Custom::BucketCleanup",
        "PhysicalResourceId": "phys-1",
        "ResponseURL": "https://cfn-response.example/put",
        "ResourceProperties": {"Buckets": buckets},
    }


def _mock_stack_status(mocker, status):
    """Patch cfn.describe_stacks to report *status* for the parent stack."""
    mocker.patch.object(index.cfn, "describe_stacks",
                        return_value={"Stacks": [{"StackStatus": status}]})


class TestDeleteFlow:
    """Real teardown path — parent stack in DELETE_IN_PROGRESS."""

    def test_empties_each_bucket_and_signals_success(self, mocker):
        _mock_stack_status(mocker, "DELETE_IN_PROGRESS")
        # ``paginate`` is called once per bucket, so return a fresh iterable
        # per call (a bare iter would be exhausted after the first bucket
        # and the second one would look mysteriously empty).
        list_paginator = mocker.MagicMock()
        list_paginator.paginate.side_effect = lambda **kw: iter([
            {"Contents": [{"Key": "a"}, {"Key": "b"}]},
        ])
        version_paginator = mocker.MagicMock()
        version_paginator.paginate.side_effect = lambda **kw: iter([{}])

        def _get_paginator(name):
            return list_paginator if name == "list_objects_v2" else version_paginator

        mocker.patch.object(index.s3, "get_paginator", side_effect=_get_paginator)
        delete_objects = mocker.patch.object(index.s3, "delete_objects")
        send = mocker.patch.object(index.cfnresponse, "send")

        index.handler(_delete_event(["ui-bucket", "results-bucket"]), mocker.MagicMock())

        # Both buckets emptied.
        assert delete_objects.call_count == 2
        first_call, second_call = delete_objects.call_args_list
        assert first_call.kwargs["Bucket"] == "ui-bucket"
        assert second_call.kwargs["Bucket"] == "results-bucket"

        # CloudFormation got SUCCESS.
        send.assert_called_once()
        assert send.call_args.args[2] == index.cfnresponse.SUCCESS

    def test_deletes_versions_and_delete_markers_on_versioned_bucket(self, mocker):
        _mock_stack_status(mocker, "DELETE_IN_PROGRESS")
        list_paginator = mocker.MagicMock()
        list_paginator.paginate.return_value = iter([{}])  # no live objects
        version_paginator = mocker.MagicMock()
        version_paginator.paginate.return_value = iter([{
            "Versions": [{"Key": "old", "VersionId": "v1"}],
            "DeleteMarkers": [{"Key": "old", "VersionId": "vdm"}],
        }])

        def _get_paginator(name):
            return list_paginator if name == "list_objects_v2" else version_paginator

        mocker.patch.object(index.s3, "get_paginator", side_effect=_get_paginator)
        delete_objects = mocker.patch.object(index.s3, "delete_objects")
        mocker.patch.object(index.cfnresponse, "send")

        index.handler(_delete_event(["b"]), mocker.MagicMock())

        # Versions + markers must be included so the delete actually clears
        # a versioned bucket (a plain list_objects_v2 sweep would miss them).
        delete_objects.assert_called_once()
        payload = delete_objects.call_args.kwargs["Delete"]["Objects"]
        keys = {(o["Key"], o["VersionId"]) for o in payload}
        assert keys == {("old", "v1"), ("old", "vdm")}

    def test_no_such_bucket_is_not_an_error(self, mocker):
        _mock_stack_status(mocker, "DELETE_IN_PROGRESS")

        class _NoSuchBucket(Exception):
            pass

        # boto3 attaches exception classes lazily on the client — patch via the
        # mocker so the original is restored on teardown and this test does
        # not leak a fake exception class to any later test in the same
        # process.
        mocker.patch.object(
            index.s3.exceptions, "NoSuchBucket", _NoSuchBucket, create=True,
        )
        mocker.patch.object(index.s3, "get_paginator", side_effect=_NoSuchBucket())
        send = mocker.patch.object(index.cfnresponse, "send")

        index.handler(_delete_event(["gone-bucket"]), mocker.MagicMock())

        # Missing bucket during a repeated delete is expected; must still SUCCESS.
        send.assert_called_once()
        assert send.call_args.args[2] == index.cfnresponse.SUCCESS

    def test_always_signals_success_on_delete_even_when_something_else_raises(
        self, mocker,
    ):
        _mock_stack_status(mocker, "DELETE_IN_PROGRESS")
        # If the Lambda ever returned FAILED on Delete, the stack would be
        # stuck in DELETE_FAILED and require manual intervention. Loud in
        # CloudWatch is fine; blocking the stack is not.
        mocker.patch.object(index.s3, "get_paginator",
                            side_effect=RuntimeError("boom"))
        send = mocker.patch.object(index.cfnresponse, "send")

        index.handler(_delete_event(["b"]), mocker.MagicMock())

        send.assert_called_once()
        assert send.call_args.args[2] == index.cfnresponse.SUCCESS

    def test_also_empties_during_rollback_of_a_failed_initial_create(self, mocker):
        # ROLLBACK_IN_PROGRESS = a fresh CREATE failed and CFN is undoing
        # everything. The whole stack ends up gone, so treating this like a
        # delete is safe (and matches the buckets' fate).
        _mock_stack_status(mocker, "ROLLBACK_IN_PROGRESS")
        list_paginator = mocker.MagicMock()
        list_paginator.paginate.side_effect = lambda **kw: iter([
            {"Contents": [{"Key": "a"}]},
        ])
        version_paginator = mocker.MagicMock()
        version_paginator.paginate.side_effect = lambda **kw: iter([{}])
        mocker.patch.object(index.s3, "get_paginator",
                            side_effect=lambda n: list_paginator if n == "list_objects_v2"
                            else version_paginator)
        delete_objects = mocker.patch.object(index.s3, "delete_objects")
        mocker.patch.object(index.cfnresponse, "send")

        index.handler(_delete_event(["b"]), mocker.MagicMock())
        delete_objects.assert_called_once()


class TestSkipOnStackUpdate:
    """Data-loss guard: a Delete event during an UPDATE (someone toggled
    ``AutoEmptyBucketsOnDelete`` from ``true`` to ``false`` and ran
    ``sam deploy``) must NOT touch the buckets. The parent stack is
    UPDATE_IN_PROGRESS, the buckets are staying, wiping them would
    silently destroy task results."""

    def test_update_delete_does_not_touch_buckets(self, mocker):
        _mock_stack_status(mocker, "UPDATE_IN_PROGRESS")
        get_paginator = mocker.patch.object(index.s3, "get_paginator")
        delete_objects = mocker.patch.object(index.s3, "delete_objects")
        send = mocker.patch.object(index.cfnresponse, "send")

        index.handler(_delete_event(["ui-bucket", "results-bucket"]),
                      mocker.MagicMock())

        # Nothing about the buckets was touched.
        get_paginator.assert_not_called()
        delete_objects.assert_not_called()
        # CloudFormation still got SUCCESS so the resource gets removed cleanly.
        assert send.call_args.args[2] == index.cfnresponse.SUCCESS
        # And the reason is visible in the response Data so an operator can
        # spot the guard firing in CloudWatch/CFN events.
        assert "Skipped" in send.call_args.args[3]

    def test_update_rollback_also_does_not_touch_buckets(self, mocker):
        # UPDATE_ROLLBACK_IN_PROGRESS = an update failed, CFN is undoing it.
        # The stack STAYS. Buckets STAY. Don't wipe them.
        _mock_stack_status(mocker, "UPDATE_ROLLBACK_IN_PROGRESS")
        get_paginator = mocker.patch.object(index.s3, "get_paginator")
        delete_objects = mocker.patch.object(index.s3, "delete_objects")
        mocker.patch.object(index.cfnresponse, "send")

        index.handler(_delete_event(["b"]), mocker.MagicMock())

        get_paginator.assert_not_called()
        delete_objects.assert_not_called()

    def test_describe_stacks_failure_defaults_to_skip(self, mocker):
        # Fail-safe: if we can't confirm the stack is being deleted, treat
        # it as "not being deleted" and skip. A non-empty bucket that blocks
        # `sam delete` is a fixable annoyance; wiping a real bucket by
        # accident is not.
        mocker.patch.object(index.cfn, "describe_stacks",
                            side_effect=RuntimeError("throttling"))
        get_paginator = mocker.patch.object(index.s3, "get_paginator")
        delete_objects = mocker.patch.object(index.s3, "delete_objects")
        send = mocker.patch.object(index.cfnresponse, "send")

        index.handler(_delete_event(["b"]), mocker.MagicMock())

        get_paginator.assert_not_called()
        delete_objects.assert_not_called()
        assert send.call_args.args[2] == index.cfnresponse.SUCCESS

    def test_empty_stack_id_defaults_to_skip(self, mocker):
        # If StackId is missing from the event (should never happen in real
        # CFN, but guard against a malformed event) — skip, don't wipe.
        get_paginator = mocker.patch.object(index.s3, "get_paginator")
        delete_objects = mocker.patch.object(index.s3, "delete_objects")
        mocker.patch.object(index.cfnresponse, "send")

        index.handler(_delete_event(["b"], stack_id=""), mocker.MagicMock())

        get_paginator.assert_not_called()
        delete_objects.assert_not_called()


class TestCreateAndUpdateAreNoOps:
    def test_create_touches_no_bucket_and_signals_success(self, mocker):
        get_paginator = mocker.patch.object(index.s3, "get_paginator")
        send = mocker.patch.object(index.cfnresponse, "send")

        index.handler({"RequestType": "Create", "ResourceProperties": {"Buckets": ["b"]}},
                      mocker.MagicMock())

        # Never touch the real buckets on Create — the resource exists only
        # to receive the Delete callback later.
        get_paginator.assert_not_called()
        assert send.call_args.args[2] == index.cfnresponse.SUCCESS

    def test_update_touches_no_bucket(self, mocker):
        get_paginator = mocker.patch.object(index.s3, "get_paginator")
        send = mocker.patch.object(index.cfnresponse, "send")

        index.handler({"RequestType": "Update", "ResourceProperties": {"Buckets": ["b"]}},
                      mocker.MagicMock())

        get_paginator.assert_not_called()
        assert send.call_args.args[2] == index.cfnresponse.SUCCESS
