"""Tests for polyris-up / polyris-down (polyris/up.py, ADR #131).

Boundaries (Principle #14): S3, STS and DynamoDB run on moto; the release
download is mocked at ``urllib.request.urlopen``. CloudFormation is a small
fake client, because moto cannot expand the SAM transform or run change sets
— the fake records every call so the tests can assert the exact request
polyris-up sends. Step Functions, where a test of the move from 1.2 to 1.3
needs executions, is ``fake_states.FakeStates`` behind a real boto3 client.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from fake_states import FakeStates, answering, refuse
from polyris import __version__
from polyris import up, upgrade
from polyris.platform import (
    PLATFORM_TAG_KEY,
    PLATFORM_TAG_VALUE,
    Manifest,
    artifacts_bucket_name,
    release_asset_name,
)

ACCOUNT = "123456789012"  # moto's default account
REGION = "eu-central-1"
#: An older platform that already runs 1.3's engine, by its version or, for a stack that names none,
#: by the output 1.3 adds. An update of one makes no checks of the move from 1.2 (ADR #137, *Migration*),
#: which the tests under "The move from 1.2 to 1.3" cover.
OLDER = "1.3.0a1"
ROWS_OUTPUTS = [{"OutputKey": "StartRunFunctionArn",
                 "OutputValue": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:acme-dev-polyris-start-run"}]


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def aws(monkeypatch):
    for var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent")
    with mock_aws():
        yield


@pytest.fixture
def s3(aws):
    return boto3.client("s3", region_name=REGION)


@pytest.fixture(autouse=True)
def no_tty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch, tmp_path):
    """Archive extraction goes to XDG_CACHE_HOME — never the developer's real ~/.cache."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """The stop of 1.2's executions lists again at once, and the wait for the index asks again at once."""
    monkeypatch.setattr(upgrade, "STOP_SETTLE_S", 0)
    monkeypatch.setattr(upgrade, "INDEX_POLL_S", 0)


def _template() -> Dict[str, Any]:
    return {
        "Transform": "AWS::Serverless-2016-10-31",
        "Description": "Polyris — source",
        "Parameters": {k: {"Type": "String", "Default": ""} for k in (
            "Namespace", "Stage", "EnableCognitoAuth", "AdminEmail", "ArtifactsBucket",
            "UiArtifactsPrefix", "EnablePitr")},
        "Resources": {
            "ConsoleApiFunction": {"Type": "AWS::Serverless::Function", "Properties": {"CodeUri": "lambdas/x/"}},
            "PauseWaiterSfn": {"Type": "AWS::Serverless::StateMachine",
                               "Properties": {"DefinitionUri": "sfn_templates/p.json"}},
        },
    }


def _write_archive(path: Path, version: str = __version__, extra: Dict[str, bytes] | None = None) -> Path:
    manifest = Manifest(version=version, template="template.json", ui="ui",
                        functions={"ConsoleApiFunction": "lambdas/ConsoleApiFunction.zip"},
                        state_machines={"PauseWaiterSfn": "sfn/PauseWaiterSfn.json"})
    files = {
        "manifest.json": json.dumps(manifest.to_dict()).encode(),
        "template.json": json.dumps(_template()).encode(),
        "lambdas/ConsoleApiFunction.zip": b"zip-bytes",
        "sfn/PauseWaiterSfn.json": b"{}",
        "ui/index.html": b"<html>",
        "ui/_next/static/app.js": b"js",
    }
    files.update(extra or {})
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


class FakeCfn:
    """Records calls; answers like CloudFormation for the paths polyris-up uses.

    ``updated`` is what an executed change set changes of its stack (its Description, its Outputs),
    and ``progress`` the statuses the stack passes through after it, one per describe_stacks, before
    it settles. ``on_wait`` is called with the waiter's name whenever polyris-up waits for a stack.
    """

    def __init__(self, stacks: List[Dict[str, Any]] | None = None, change_status: str = "CREATE_COMPLETE",
                 status_reason: str = "", changes: List[Dict[str, Any]] | None = None,
                 updated: Dict[str, Any] | None = None, progress: List[str] | None = None):
        self.stacks = {s["StackName"]: s for s in (stacks or [])}
        self.change_status = change_status
        self.status_reason = status_reason
        self.changes = changes or [{"ResourceChange": {"Action": "Add", "ResourceType": "AWS::Lambda::Function"}}]
        self.updated = updated or {}
        self.progress = list(progress or [])
        self._progress: List[str] = []
        self.on_wait = None
        self.calls: List[tuple] = []

    # describe_stacks / paginator
    def describe_stacks(self, StackName=None):
        self.calls.append(("describe_stacks", StackName))
        if StackName is None:
            return {"Stacks": list(self.stacks.values())}
        if StackName not in self.stacks:
            raise ClientError({"Error": {"Code": "ValidationError",
                                         "Message": f"Stack with id {StackName} does not exist"}}, "DescribeStacks")
        stack = self.stacks[StackName]
        if self._progress:
            stack = {**stack, "StackStatus": self._progress.pop(0)}
        return {"Stacks": [stack]}

    def get_paginator(self, name):
        fake = self

        class _P:
            def paginate(self, **kw):
                if name == "describe_stacks":
                    return [fake.describe_stacks()]
                if name == "list_change_sets":
                    return [{"Summaries": fake.stacks[kw["StackName"]].get("_change_sets", [])}]
                return [{"StackResourceSummaries": fake.stacks[kw["StackName"]].get("_resources", [])}]
        return _P()

    def describe_stack_events(self, StackName):
        return {"StackEvents": []}

    def create_change_set(self, **kw):
        self.calls.append(("create_change_set", kw))
        self.last_change_set = kw

    def describe_change_set(self, StackName, ChangeSetName):
        return {"Status": self.change_status, "StatusReason": self.status_reason,
                "ChangeSetName": ChangeSetName, "Changes": self.changes}

    def delete_change_set(self, **kw):
        self.calls.append(("delete_change_set", kw))

    def execute_change_set(self, **kw):
        self.calls.append(("execute_change_set", kw))
        kwargs = self.last_change_set
        previous = self.stacks.get(kw["StackName"], {})
        self.stacks[kw["StackName"]] = {
            **{key: previous[key] for key in ("Parameters", "Description") if key in previous},
            "StackName": kw["StackName"], "Tags": kwargs["Tags"],
            "StackStatus": f"{kwargs['ChangeSetType']}_COMPLETE",
            "Outputs": [{"OutputKey": "ConsoleUiUrl", "OutputValue": "https://d123.cloudfront.net"}],
            **self.updated,
        }
        self._progress = list(self.progress)

    def delete_stack(self, StackName):
        self.calls.append(("delete_stack", StackName))
        self.stacks.pop(StackName)

    def get_waiter(self, name):
        fake = self

        class _W:
            def wait(self, **kw):
                fake.calls.append(("wait", name))
                if fake.on_wait:
                    fake.on_wait(name)
                fake._progress = []
                for stack in fake.stacks.values():       # the stack settles
                    if stack.get("StackStatus") == "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS":
                        stack["StackStatus"] = "UPDATE_COMPLETE"
        return _W()

    def names(self) -> List[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def fake_cfn(aws, mocker):
    """Route the session's CloudFormation client to a FakeCfn; S3/STS stay on moto.

    A test may put its own client for another service into the holder under the
    service's name (``fake_cfn["stepfunctions"] = FakeStates().client``).
    """
    holder: Dict[str, Any] = {"cfn": FakeCfn()}
    real_client = boto3.Session.client

    def client(self, service, *a, **kw):
        if service == "cloudformation":
            return holder["cfn"]
        if service in holder:
            return holder[service]
        return real_client(self, service, *a, **kw)

    mocker.patch.object(boto3.Session, "client", client)
    return holder


def _up_args(**over) -> argparse.Namespace:
    args = up.build_up_parser().parse_args([])
    args.region = REGION
    for k, v in over.items():
        setattr(args, k, v)
    return args


# ── Session / target ─────────────────────────────────────────────────────────


def test_session_requires_a_region(monkeypatch):
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent")  # it can hold a region too
    with pytest.raises(up.UpError, match="No AWS region"):
        up._session(None, None)


def test_session_unknown_profile(monkeypatch):
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent")
    with pytest.raises(up.UpError, match="could not be found"):
        up._session("nope", REGION)


def test_session_resolves_account(aws):
    _, region, account = up._session(None, REGION)
    assert (region, account) == (REGION, ACCOUNT)


# ── Stack discovery ──────────────────────────────────────────────────────────


def _platform(name: str, **extra) -> Dict[str, Any]:
    return {"StackName": name, "Tags": [{"Key": PLATFORM_TAG_KEY, "Value": PLATFORM_TAG_VALUE}], **extra}


def test_find_platform_stack_by_tag_and_explicit():
    cfn = FakeCfn([_platform("acme-dev"), {"StackName": "unrelated", "Tags": []}])
    assert up.find_platform_stack(cfn, None)[0] == "acme-dev"
    assert up.find_platform_stack(cfn, "unrelated")[0] == "unrelated"
    assert up.find_platform_stack(cfn, "missing") == ("missing", None)
    assert up.find_platform_stack(FakeCfn(), None) == (None, None)


def test_find_platform_stack_refuses_to_guess():
    cfn = FakeCfn([_platform("acme-dev"), _platform("acme-prod")])
    with pytest.raises(up.UpError, match="Several Polyris platform stacks"):
        up.find_platform_stack(cfn, None)


# ── S3 helpers ───────────────────────────────────────────────────────────────


def test_ensure_artifacts_bucket_creates_private_encrypted_bucket(s3):
    up.ensure_artifacts_bucket(s3, "polyris-artifacts-1-eu", region=REGION, owner=ACCOUNT)
    up.ensure_artifacts_bucket(s3, "polyris-artifacts-1-eu", region=REGION, owner=ACCOUNT)  # idempotent
    block = s3.get_public_access_block(Bucket="polyris-artifacts-1-eu")["PublicAccessBlockConfiguration"]
    assert all(block.values())
    enc = s3.get_bucket_encryption(Bucket="polyris-artifacts-1-eu")
    assert enc["ServerSideEncryptionConfiguration"]["Rules"][0]["ApplyServerSideEncryptionByDefault"]
    assert s3.get_bucket_location(Bucket="polyris-artifacts-1-eu")["LocationConstraint"] == REGION


def test_bucket_owner_status(s3, mocker):
    assert up.bucket_owner_status(s3, "nobody-has-this", owner=ACCOUNT) == "missing"
    s3.create_bucket(Bucket="mine", CreateBucketConfiguration={"LocationConstraint": REGION})
    assert up.bucket_owner_status(s3, "mine", owner=ACCOUNT) == "ours"
    mocker.patch.object(s3, "head_bucket", side_effect=ClientError({"Error": {"Code": "403"}}, "HeadBucket"))
    assert up.bucket_owner_status(s3, "theirs", owner=ACCOUNT) == "taken"
    with pytest.raises(up.UpError, match="taken by another AWS account"):
        up.ensure_artifacts_bucket(s3, "theirs", region=REGION, owner=ACCOUNT)


def test_upload_artifacts_is_content_addressed_and_idempotent(s3, tmp_path, capsys):
    root, manifest = up.extract_archive(_write_archive(tmp_path / "a.zip"))
    bucket = "polyris-artifacts"
    s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION})

    fkeys, skeys, prefix = up.upload_artifacts(s3, bucket, root, manifest, owner=ACCOUNT)
    first = capsys.readouterr().out.strip()
    again = up.upload_artifacts(s3, bucket, root, manifest, owner=ACCOUNT)
    second = capsys.readouterr().out.strip()

    assert again == (fkeys, skeys, prefix)
    # 1 Lambda zip + 1 SFN definition + 2 UI files
    assert first == "artifacts: 4 uploaded"
    assert second == "artifacts: 0 uploaded, 4 already in the bucket"
    digest = hashlib.sha256(b"zip-bytes").hexdigest()[:16]
    assert fkeys == {"ConsoleApiFunction": f"v{__version__}/lambdas/{digest}/ConsoleApiFunction.zip"}
    keys = {o["Key"] for o in s3.list_objects_v2(Bucket=bucket)["Contents"]}
    assert {f"{prefix}/index.html", f"{prefix}/_next/static/app.js", *fkeys.values(), *skeys.values()} == keys


def test_empty_and_count_versioned_bucket(s3):
    s3.create_bucket(Bucket="versioned-bkt", CreateBucketConfiguration={"LocationConstraint": REGION})
    s3.put_bucket_versioning(Bucket="versioned-bkt", VersioningConfiguration={"Status": "Enabled"})
    s3.put_object(Bucket="versioned-bkt", Key="k", Body=b"1")
    s3.put_object(Bucket="versioned-bkt", Key="k", Body=b"2")
    s3.delete_object(Bucket="versioned-bkt", Key="k")
    assert up.count_objects(s3, "versioned-bkt", owner=ACCOUNT) == 3
    assert up.empty_bucket(s3, "versioned-bkt", owner=ACCOUNT) == 3
    assert up.count_objects(s3, "versioned-bkt", owner=ACCOUNT) == 0
    assert up.count_objects(s3, "gone", owner=ACCOUNT) == 0
    assert up.empty_bucket(s3, "gone", owner=ACCOUNT) == 0


# ── Release archive ──────────────────────────────────────────────────────────


def test_extract_archive_rejects_path_traversal(tmp_path):
    archive = _write_archive(tmp_path / "evil.zip", extra={"../escape.txt": b"x"})
    with pytest.raises(up.UpError, match="escapes the archive root"):
        up.extract_archive(archive)


def test_extract_archive_rejects_bad_manifest(tmp_path):
    archive = _write_archive(tmp_path / "bad.zip", extra={"manifest.json": b'{"format": 99}'})
    with pytest.raises(up.UpError, match="Invalid release archive"):
        up.extract_archive(archive)


def _serve(mocker, files: Dict[str, bytes]):
    def urlopen(url, timeout=None):
        name = url.rsplit("/", 1)[-1]
        if name not in files:
            raise OSError(f"HTTP 404 for {url}")
        return io.BytesIO(files[name])
    return mocker.patch("urllib.request.urlopen", side_effect=urlopen)


def test_fetch_release_downloads_verifies_and_caches(tmp_path, monkeypatch, mocker):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    data = _write_archive(tmp_path / "src.zip").read_bytes()
    name = release_asset_name("9.9.9")
    opener = _serve(mocker, {name: data, f"{name}.sha256": f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode()})

    path = up.fetch_release("9.9.9")
    assert path == tmp_path / "cache" / "polyris" / "9.9.9" / name
    assert path.read_bytes() == data
    up.fetch_release("9.9.9")
    assert opener.call_count == 2, "second call uses the cache"


def test_fetch_release_checksum_mismatch(tmp_path, monkeypatch, mocker):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    name = release_asset_name("9.9.9")
    _serve(mocker, {name: b"tampered", f"{name}.sha256": b"0" * 64})
    with pytest.raises(up.UpError, match="Checksum mismatch"):
        up.fetch_release("9.9.9")
    assert not (tmp_path / "polyris" / "9.9.9" / name).exists()


def test_fetch_release_missing_release(tmp_path, monkeypatch, mocker):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _serve(mocker, {})
    with pytest.raises(up.UpError, match="Could not download the v9.9.9 platform release"):
        up.fetch_release("9.9.9")


# ── Tags / data-loss guard ──────────────────────────────────────────────────────────────


def test_platform_tags_keep_existing_tags_and_set_platform_tag():
    assert up.platform_tags({"Tags": [{"Key": "team", "Value": "data"},
                                      {"Key": PLATFORM_TAG_KEY, "Value": "old"}]}) == [
        {"Key": "team", "Value": "data"}, {"Key": PLATFORM_TAG_KEY, "Value": PLATFORM_TAG_VALUE}]
    assert up.platform_tags(None) == [{"Key": PLATFORM_TAG_KEY, "Value": PLATFORM_TAG_VALUE}]


def test_replaced_data_resources():
    cs = {"Changes": [
        {"ResourceChange": {"LogicalResourceId": "T", "ResourceType": "AWS::DynamoDB::Table",
                            "Action": "Modify", "Replacement": "True"}},
        {"ResourceChange": {"LogicalResourceId": "B", "ResourceType": "AWS::S3::Bucket", "Action": "Remove"}},
        {"ResourceChange": {"LogicalResourceId": "OkT", "ResourceType": "AWS::DynamoDB::Table",
                            "Action": "Modify", "Replacement": "False"}},
        {"ResourceChange": {"LogicalResourceId": "F", "ResourceType": "AWS::Lambda::Function",
                            "Action": "Modify", "Replacement": "True"}},
        # Changing AdminEmail replaces (deletes) the previous admin user.
        {"ResourceChange": {"LogicalResourceId": "CognitoAdminUser", "ResourceType": "AWS::Cognito::UserPoolUser",
                            "Action": "Modify", "Replacement": "True"}},
    ]}
    assert up.replaced_data_resources(cs) == ["T (AWS::DynamoDB::Table, Modify)", "B (AWS::S3::Bucket, Remove)",
                                              "CognitoAdminUser (AWS::Cognito::UserPoolUser, Modify)"]


# ── polyris-up end to end (fake CloudFormation) ──────────────────────────────


def _params(change_set: Dict[str, Any]) -> Dict[str, Any]:
    return {p["ParameterKey"]: p.get("ParameterValue", "<previous>") for p in change_set["Parameters"]}


def test_up_new_install(fake_cfn, s3, tmp_path, capsys):
    archive = _write_archive(tmp_path / "a.zip")
    args = _up_args(namespace="acme", stage="dev", admin_email="me@acme.com", archive=str(archive))

    assert up._run(up.run_up, args) == 0

    cs = fake_cfn["cfn"].last_change_set
    params = _params(cs)
    assert cs["StackName"] == "acme-dev" and cs["ChangeSetType"] == "CREATE"
    assert params["Namespace"] == "acme" and params["Stage"] == "dev" and params["AdminEmail"] == "me@acme.com"
    assert params["ArtifactsBucket"] == artifacts_bucket_name(ACCOUNT, REGION)
    assert params["UiArtifactsPrefix"].startswith(f"v{__version__}/ui-")
    assert cs["TemplateURL"].startswith(f"https://{artifacts_bucket_name(ACCOUNT, REGION)}.s3.{REGION}.amazonaws.com/")
    out = capsys.readouterr().out
    assert "https://d123.cloudfront.net" in out and "polyris-init hello-world --hello" in out

    # The uploaded template points every local path at S3 and records the version.
    key = cs["TemplateURL"].split(".amazonaws.com/", 1)[1]
    body = json.loads(s3.get_object(Bucket=artifacts_bucket_name(ACCOUNT, REGION), Key=key)["Body"].read())
    assert body["Description"] == f"Polyris platform v{__version__}"
    assert body["Resources"]["ConsoleApiFunction"]["Properties"]["CodeUri"]["Bucket"] == params["ArtifactsBucket"]


def test_up_names_the_version_the_archive_installs(fake_cfn, s3, tmp_path, capsys):
    """An --archive of another version (a branch build) is the version installed, and the
    plan says so: it printed this polyris's own version, which read like a rollback."""
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="UPDATE_COMPLETE", Description="Polyris platform v9.0.0",
                                         Parameters=[{"ParameterKey": "Namespace", "ParameterValue": "acme"}])])
    args = _up_args(archive=str(_write_archive(tmp_path / "a.zip", version="9.1.0")))

    assert up._run(up.run_up, args) == 0

    assert "(update, 9.0.0 → 9.1.0)" in capsys.readouterr().out


def test_up_new_install_names_the_archive_version(fake_cfn, s3, tmp_path, capsys):
    args = _up_args(namespace="acme", stage="dev", admin_email="", archive=str(_write_archive(tmp_path / "a.zip", version="9.1.0")))

    assert up._run(up.run_up, args) == 0

    assert "(create, v9.1.0)" in capsys.readouterr().out


def test_up_update_keeps_previous_parameters(fake_cfn, s3, tmp_path):
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="UPDATE_COMPLETE", Description=f"Polyris platform v{OLDER}",
                                         Parameters=[{"ParameterKey": "Namespace", "ParameterValue": "acme"},
                                                     {"ParameterKey": "AdminEmail", "ParameterValue": "a@b.c"}])])
    args = _up_args(archive=str(_write_archive(tmp_path / "a.zip")), param=["EnablePitr=true"])

    assert up._run(up.run_up, args) == 0

    cs = fake_cfn["cfn"].last_change_set
    assert cs["ChangeSetType"] == "UPDATE"
    params = _params(cs)
    assert params["Namespace"] == "<previous>" and params["AdminEmail"] == "<previous>"
    assert params["EnablePitr"] == "true"


def test_up_disable_rollback_keeps_a_failed_update_and_says_how_to_go_on(fake_cfn, s3, tmp_path, capsys, mocker):
    """ADR #130 D13: an upgrade that fails is fixed forward, so it must not roll back."""
    from botocore.exceptions import WaiterError

    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="UPDATE_COMPLETE", Description=f"Polyris platform v{OLDER}",
                                         Parameters=[{"ParameterKey": "Namespace", "ParameterValue": "acme"}])])
    args = _up_args(archive=str(_write_archive(tmp_path / "a.zip")), disable_rollback=True)
    mocker.patch("polyris.cfn_changeset.wait_for_stack",
                 side_effect=WaiterError("StackUpdateComplete", "UPDATE_FAILED", {}))

    assert up._run(up.run_up, args) == 1

    [execute] = [kw for name, kw in fake_cfn["cfn"].calls if name == "execute_change_set"]
    assert execute["DisableRollback"] is True
    captured = capsys.readouterr()
    assert "Nothing was rolled back (--disable-rollback)" in captured.out + captured.err


def test_up_rolls_back_a_failed_update_by_default(fake_cfn, s3, tmp_path):
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="UPDATE_COMPLETE", Description=f"Polyris platform v{OLDER}",
                                         Parameters=[{"ParameterKey": "Namespace", "ParameterValue": "acme"}])])
    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 0
    [execute] = [kw for name, kw in fake_cfn["cfn"].calls if name == "execute_change_set"]
    assert "DisableRollback" not in execute


def test_up_on_a_failed_update_with_nothing_to_change_is_not_up_to_date(fake_cfn, s3, tmp_path, capsys):
    """A stack left UPDATE_FAILED by --disable-rollback stays half-updated until its failure is retried."""
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="UPDATE_FAILED", Description=f"Polyris platform v{OLDER}",
                                         Parameters=[{"ParameterKey": "Namespace", "ParameterValue": "acme"}])],
                              change_status="FAILED", status_reason="didn't contain changes")

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")), disable_rollback=True)) == 1

    captured = capsys.readouterr()
    assert "already up to date" not in captured.out
    assert "UPDATE_FAILED" in captured.out + captured.err


def test_up_never_keeps_a_failed_first_install(fake_cfn, s3, tmp_path, capsys, mocker):
    """A first install that fails is removed and run again; only an update keeps what it changed."""
    from botocore.exceptions import WaiterError

    args = _up_args(archive=str(_write_archive(tmp_path / "a.zip")), namespace="acme", stage="dev",
                    admin_email="a@b.c", disable_rollback=True)
    mocker.patch("polyris.cfn_changeset.wait_for_stack",
                 side_effect=WaiterError("StackCreateComplete", "ROLLBACK_COMPLETE", {}))

    assert up._run(up.run_up, args) == 1

    [execute] = [kw for name, kw in fake_cfn["cfn"].calls if name == "execute_change_set"]
    assert "DisableRollback" not in execute
    captured = capsys.readouterr()
    assert "Nothing was rolled back" not in captured.out + captured.err      # it was rolled back: say so, not this


def test_up_no_changes(fake_cfn, s3, tmp_path, capsys):
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="UPDATE_COMPLETE", Parameters=[],
                                         Outputs=ROWS_OUTPUTS)],
                              change_status="FAILED", status_reason="didn't contain changes")
    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 0
    assert "already up to date" in capsys.readouterr().out
    assert "execute_change_set" not in fake_cfn["cfn"].names()


def test_up_refuses_taken_bucket_name_before_touching_anything(fake_cfn, s3, tmp_path, capsys):
    s3.create_bucket(Bucket="acme-dev-polyris-results", CreateBucketConfiguration={"LocationConstraint": REGION})
    args = _up_args(namespace="acme", stage="dev", admin_email="", archive=str(_write_archive(tmp_path / "a.zip")))

    assert up._run(up.run_up, args) == 1

    assert "already exists" in capsys.readouterr().err
    assert "create_change_set" not in fake_cfn["cfn"].names()


def test_up_validates_namespace(fake_cfn, tmp_path, capsys):
    args = _up_args(namespace="Acme_Corp", stage="dev", archive=str(_write_archive(tmp_path / "a.zip")))
    assert up._run(up.run_up, args) == 1
    assert "lowercase" in capsys.readouterr().err


def test_up_checks_the_archive_before_asking_anything(fake_cfn, tmp_path, capsys):
    args = _up_args(namespace="acme", stage="dev", archive=str(tmp_path / "missing.zip"))
    assert up._run(up.run_up, args) == 1
    assert "Archive not found" in capsys.readouterr().err


def test_up_without_tty_needs_namespace(fake_cfn, capsys):
    assert up._run(up.run_up, _up_args()) == 1
    assert "stdin is not a terminal" in capsys.readouterr().err


def test_up_refuses_busy_stack(fake_cfn, capsys):
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="UPDATE_IN_PROGRESS")])
    assert up._run(up.run_up, _up_args()) == 1
    assert "busy" in capsys.readouterr().err


def test_up_data_replacement_needs_confirmation(fake_cfn, s3, tmp_path, capsys):
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="UPDATE_COMPLETE", Parameters=[],
                                         Outputs=ROWS_OUTPUTS)], changes=[
        {"ResourceChange": {"LogicalResourceId": "PipelineTokensTable", "ResourceType": "AWS::DynamoDB::Table",
                            "Action": "Modify", "Replacement": "True"}}])
    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 1
    assert "PipelineTokensTable" in capsys.readouterr().out
    assert "execute_change_set" not in fake_cfn["cfn"].names()


# ── polyris-down ─────────────────────────────────────────────────────────────


def _down_args(**over) -> argparse.Namespace:
    args = up.build_down_parser().parse_args([])
    args.region = REGION
    for k, v in over.items():
        setattr(args, k, v)
    return args


def _stack_with_buckets(s3, results_objects: int) -> Dict[str, Any]:
    for b in ("acme-dev-polyris-results", "acme-dev-polyris-console-ui"):
        s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": REGION})
    for i in range(results_objects):
        s3.put_object(Bucket="acme-dev-polyris-results", Key=f"r{i}", Body=b"x")
    s3.put_object(Bucket="acme-dev-polyris-console-ui", Key="index.html", Body=b"x")
    return _platform("acme-dev", _resources=[
        {"LogicalResourceId": "ResultsBucket", "ResourceType": "AWS::S3::Bucket",
         "PhysicalResourceId": "acme-dev-polyris-results"},
        {"LogicalResourceId": "ConsoleUiBucket", "ResourceType": "AWS::S3::Bucket",
         "PhysicalResourceId": "acme-dev-polyris-console-ui"},
        {"LogicalResourceId": "PipelineTokensTable", "ResourceType": "AWS::DynamoDB::Table",
         "PhysicalResourceId": "acme-dev-polyris-pipeline-tokens"},
    ])


def test_down_refuses_when_results_hold_data(fake_cfn, s3, capsys):
    fake_cfn["cfn"] = FakeCfn([_stack_with_buckets(s3, results_objects=2)])
    assert up._run(up.run_down, _down_args(yes=True)) == 1
    assert "holds 2 task results" in capsys.readouterr().err
    assert "delete_stack" not in fake_cfn["cfn"].names()
    assert up.count_objects(s3, "acme-dev-polyris-console-ui", owner=ACCOUNT) == 1, "nothing emptied"


def test_down_with_delete_data_empties_and_deletes(fake_cfn, s3, capsys):
    fake_cfn["cfn"] = FakeCfn([_stack_with_buckets(s3, results_objects=2)])
    up.ensure_artifacts_bucket(s3, artifacts_bucket_name(ACCOUNT, REGION), region=REGION, owner=ACCOUNT)

    assert up._run(up.run_down, _down_args(yes=True, delete_data=True)) == 0

    assert "delete_stack" in fake_cfn["cfn"].names()
    assert up.count_objects(s3, "acme-dev-polyris-results", owner=ACCOUNT) == 0
    assert up.bucket_owner_status(s3, artifacts_bucket_name(ACCOUNT, REGION), owner=ACCOUNT) == "missing"
    assert "1 DynamoDB tables" in capsys.readouterr().out


def test_down_stops_the_platform_s_running_executions_before_it_empties_and_deletes_anything(fake_cfn, s3, capsys):
    """A state machine that CloudFormation deletes waits for its running executions (the review of the branch, B-1)."""
    states = FakeStates()
    run_task = states.machine("acme-dev-polyris-dep-run-task-helper")
    states.run("acme-dev-polyris-dep-run-task-helper", "task-run-1", started=datetime(2026, 11, 24, tzinfo=timezone.utc))
    stack = _stack_with_buckets(s3, results_objects=0)
    stack["_resources"].append({"LogicalResourceId": "RunTaskStateMachine",
                                "ResourceType": "AWS::StepFunctions::StateMachine", "PhysicalResourceId": run_task})
    fake_cfn["cfn"] = FakeCfn([stack])
    fake_cfn["stepfunctions"] = states.client
    at_stop: List[Tuple[bool, int]] = []
    states.on_stop = lambda _: at_stop.append(("delete_stack" in fake_cfn["cfn"].names(),
                                               up.count_objects(s3, "acme-dev-polyris-console-ui", owner=ACCOUNT)))

    assert up._run(up.run_down, _down_args(yes=True)) == 0

    assert states.stopped == ["task-run-1"]
    assert at_stop == [(False, 1)], "stopped before the buckets were emptied and the stack deleted"
    assert states.stops[0]["error"] == up.DOWN_STOP_ERROR
    assert "Stopped 1 running execution(s) of acme-dev-polyris-dep-run-task-helper" in capsys.readouterr().out


def test_down_keeps_artifacts_bucket_while_other_platforms_exist(fake_cfn, s3, capsys):
    fake_cfn["cfn"] = FakeCfn([_stack_with_buckets(s3, results_objects=0), _platform("acme-prod")])
    up.ensure_artifacts_bucket(s3, artifacts_bucket_name(ACCOUNT, REGION), region=REGION, owner=ACCOUNT)

    assert up._run(up.run_down, _down_args(yes=True, stack="acme-dev")) == 0

    assert up.bucket_owner_status(s3, artifacts_bucket_name(ACCOUNT, REGION), owner=ACCOUNT) == "ours"
    assert "still used by acme-prod" in capsys.readouterr().out


def test_down_without_tty_needs_yes(fake_cfn, s3, capsys):
    fake_cfn["cfn"] = FakeCfn([_stack_with_buckets(s3, results_objects=0)])
    assert up._run(up.run_down, _down_args()) == 1
    assert "Pass --yes" in capsys.readouterr().err


def test_down_nothing_to_remove(fake_cfn, capsys):
    assert up._run(up.run_down, _down_args(yes=True)) == 0
    assert "nothing to remove" in capsys.readouterr().out


def test_up_stage_flag_targets_a_new_stack_next_to_an_existing_one(fake_cfn, s3, tmp_path):
    """`polyris-up --stage prod` beside acme-dev must create acme-prod, not update acme-dev."""
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="UPDATE_COMPLETE", Parameters=[])])
    args = _up_args(namespace="acme", stage="prod", admin_email="", archive=str(_write_archive(tmp_path / "a.zip")))

    assert up._run(up.run_up, args) == 0

    cs = fake_cfn["cfn"].last_change_set
    assert cs["StackName"] == "acme-prod" and cs["ChangeSetType"] == "CREATE"


# ── Recovery and adoption (architect review, ADR #131) ───────────────────────


def test_up_finishes_an_interrupted_install(fake_cfn, s3, tmp_path):
    """A CREATE change set that never ran leaves a tagged REVIEW_IN_PROGRESS stack; retrying must work."""
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="REVIEW_IN_PROGRESS")])
    args = _up_args(namespace="acme", stage="dev", admin_email="", archive=str(_write_archive(tmp_path / "a.zip")))

    assert up._run(up.run_up, args) == 0

    cs = fake_cfn["cfn"].last_change_set
    assert cs["StackName"] == "acme-dev" and cs["ChangeSetType"] == "CREATE"
    assert _params(cs)["Namespace"] == "acme"


def test_up_failed_first_create_points_at_polyris_down(fake_cfn, capsys):
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="ROLLBACK_COMPLETE")])
    assert up._run(up.run_up, _up_args(stack="acme-dev")) == 1
    assert "polyris-down --stack acme-dev" in capsys.readouterr().err


def _sam_stack(name="legacy-dev", namespace="acme", stage="dev"):
    """A stack deployed with `sam deploy`: no platform tag, platform parameters."""
    return {"StackName": name, "StackStatus": "UPDATE_COMPLETE", "Tags": [{"Key": "team", "Value": "data"}],
            "Parameters": [{"ParameterKey": "Namespace", "ParameterValue": namespace},
                           {"ParameterKey": "Stage", "ParameterValue": stage},
                           {"ParameterKey": "EnableCognitoAuth", "ParameterValue": "true"}],
            "Outputs": ROWS_OUTPUTS}


def test_up_on_a_sam_deployed_namespace_points_at_adoption(fake_cfn, s3, tmp_path, capsys):
    fake_cfn["cfn"] = FakeCfn([_sam_stack()])
    s3.create_bucket(Bucket="acme-dev-polyris-results", CreateBucketConfiguration={"LocationConstraint": REGION})
    args = _up_args(namespace="acme", stage="dev", admin_email="", archive=str(_write_archive(tmp_path / "a.zip")))

    assert up._run(up.run_up, args) == 1

    err = capsys.readouterr().err
    assert "polyris-up --stack legacy-dev" in err
    assert "create_change_set" not in fake_cfn["cfn"].names()


def test_up_adopts_a_sam_deployed_stack(fake_cfn, s3, tmp_path):
    fake_cfn["cfn"] = FakeCfn([_sam_stack()])
    args = _up_args(stack="legacy-dev", yes=True, archive=str(_write_archive(tmp_path / "a.zip")))

    assert up._run(up.run_up, args) == 0

    cs = fake_cfn["cfn"].last_change_set
    assert cs["StackName"] == "legacy-dev" and cs["ChangeSetType"] == "UPDATE"
    params = _params(cs)
    assert params["Namespace"] == "<previous>" and params["Stage"] == "<previous>"
    assert params["ArtifactsBucket"] == artifacts_bucket_name(ACCOUNT, REGION)
    assert {"Key": PLATFORM_TAG_KEY, "Value": PLATFORM_TAG_VALUE} in cs["Tags"]
    assert {"Key": "team", "Value": "data"} in cs["Tags"], "existing tags are kept"


def test_up_adoption_without_yes_needs_a_terminal(fake_cfn, capsys):
    fake_cfn["cfn"] = FakeCfn([_sam_stack()])
    assert up._run(up.run_up, _up_args(stack="legacy-dev")) == 1
    assert "stdin is not a terminal" in capsys.readouterr().err


def test_down_refuses_while_pipelines_still_run(fake_cfn, s3, capsys):
    stack = _stack_with_buckets(s3, results_objects=0)
    stack["Parameters"] = [{"ParameterKey": "Namespace", "ParameterValue": "acme"},
                           {"ParameterKey": "Stage", "ParameterValue": "dev"}]
    fake_cfn["cfn"] = FakeCfn([stack, {"StackName": "acme-dev-polyris-orders", "Tags": []}])

    assert up._run(up.run_down, _down_args()) == 1

    err = capsys.readouterr().err
    assert "still deployed on this platform: orders" in err and "polyris-destroy" in err
    assert "delete_stack" not in fake_cfn["cfn"].names()


def test_fetch_release_empty_checksum_file(tmp_path, monkeypatch, mocker):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    name = release_asset_name("9.9.9")
    _serve(mocker, {name: b"data", f"{name}.sha256": b""})
    with pytest.raises(up.UpError, match="Checksum mismatch"):
        up.fetch_release("9.9.9")


# ── Break-review guards (ADR #131) ───────────────────────────────────────────


def _app_stack(name="acme-dev"):
    """An unrelated application stack that happens to share a name."""
    return {"StackName": name, "StackStatus": "UPDATE_COMPLETE", "Tags": [],
            "Parameters": [{"ParameterKey": "TableName", "ParameterValue": "invoices"}]}


def test_up_never_adopts_a_non_polyris_stack_even_with_yes(fake_cfn, tmp_path, capsys):
    fake_cfn["cfn"] = FakeCfn([_app_stack()])
    args = _up_args(namespace="acme", stage="dev", yes=True, archive=str(_write_archive(tmp_path / "a.zip")))

    assert up._run(up.run_up, args) == 1

    assert "not a Polyris platform stack" in capsys.readouterr().err
    assert "create_change_set" not in fake_cfn["cfn"].names()


def test_down_never_deletes_a_non_polyris_stack(fake_cfn, s3, capsys):
    s3.create_bucket(Bucket="unrelated-datalake", CreateBucketConfiguration={"LocationConstraint": REGION})
    s3.put_object(Bucket="unrelated-datalake", Key="k", Body=b"x")
    app = _app_stack("my-app")
    app["_resources"] = [{"LogicalResourceId": "Lake", "ResourceType": "AWS::S3::Bucket",
                          "PhysicalResourceId": "unrelated-datalake"}]
    fake_cfn["cfn"] = FakeCfn([app])

    assert up._run(up.run_down, _down_args(stack="my-app", yes=True)) == 1

    assert "refusing to delete" in capsys.readouterr().err
    assert up.count_objects(s3, "unrelated-datalake", owner=ACCOUNT) == 1
    assert "delete_stack" not in fake_cfn["cfn"].names()


def test_down_refuses_a_busy_stack(fake_cfn, capsys):
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="UPDATE_IN_PROGRESS")])
    assert up._run(up.run_down, _down_args(yes=True)) == 1
    assert "busy" in capsys.readouterr().err


def test_yes_does_not_cover_data_loss(fake_cfn, s3, tmp_path, capsys):
    replace_table = [{"ResourceChange": {"LogicalResourceId": "PipelineTokensTable",
                                         "ResourceType": "AWS::DynamoDB::Table",
                                         "Action": "Modify", "Replacement": "True"}}]
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="UPDATE_COMPLETE", Parameters=[],
                                         Outputs=ROWS_OUTPUTS)],
                              changes=replace_table)
    archive = str(_write_archive(tmp_path / "a.zip"))

    assert up._run(up.run_up, _up_args(yes=True, archive=archive)) == 1
    assert "--accept-data-loss" in capsys.readouterr().err
    assert "execute_change_set" not in fake_cfn["cfn"].names()

    assert up._run(up.run_up, _up_args(yes=True, accept_data_loss=True, archive=archive)) == 0
    assert "execute_change_set" in fake_cfn["cfn"].names()


def test_param_cannot_rename_the_platform(fake_cfn, capsys):
    assert up._run(up.run_up, _up_args(param=["Namespace=other"])) == 1
    assert "--param can't set Namespace" in capsys.readouterr().err


def test_interrupted_extraction_is_not_trusted(tmp_path, monkeypatch, mocker):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    archive = _write_archive(tmp_path / "a.zip")
    mocker.patch("zipfile.ZipFile.extractall", side_effect=OSError("No space left on device"))
    with pytest.raises(OSError):
        up.extract_archive(archive)
    mocker.stopall()

    root, manifest = up.extract_archive(archive)  # a clean retry extracts everything
    assert (root / "ui" / "index.html").is_file() and manifest.version == __version__
    leftovers = [p.name for p in root.parent.iterdir() if p.name.startswith(".extracting-")]
    assert leftovers == []



# ── Code-review findings (PR #51) ────────────────────────────────────────────


def test_every_artifact_request_names_the_expected_owner(s3, tmp_path, mocker):
    """A bucket with our predictable name in ANOTHER account must never be read or written."""
    root, manifest = up.extract_archive(_write_archive(tmp_path / "a.zip"))
    bucket = "polyris-artifacts-owner-check"
    s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION})
    head_object = mocker.spy(s3, "head_object")
    upload_file = mocker.spy(s3, "upload_file")

    up.upload_artifacts(s3, bucket, root, manifest, owner=ACCOUNT)

    assert head_object.call_count and all(c.kwargs["ExpectedBucketOwner"] == ACCOUNT
                                          for c in head_object.call_args_list)
    assert upload_file.call_count and all(c.kwargs["ExtraArgs"] == {"ExpectedBucketOwner": ACCOUNT}
                                          for c in upload_file.call_args_list)


def test_up_stops_when_the_artifacts_bucket_belongs_to_someone_else(fake_cfn, s3, tmp_path, mocker, capsys):
    real_head = s3.head_bucket

    def head_bucket(**kw):
        if kw["Bucket"] == artifacts_bucket_name(ACCOUNT, REGION):
            raise ClientError({"Error": {"Code": "403"}}, "HeadBucket")
        return real_head(**kw)

    session_s3 = boto3.Session.client  # patched by fake_cfn; wrap the S3 client it returns
    mocker.patch.object(boto3.Session, "client",
                        lambda self, svc, *a, **k: fake_cfn["cfn"] if svc == "cloudformation"
                        else _with_head_bucket(session_s3(self, svc, *a, **k), head_bucket, svc))
    args = _up_args(namespace="acme", stage="dev", admin_email="", archive=str(_write_archive(tmp_path / "a.zip")))

    assert up._run(up.run_up, args) == 1

    assert "taken by another AWS account" in capsys.readouterr().err
    assert "create_change_set" not in fake_cfn["cfn"].names()


def _with_head_bucket(client, head_bucket, service):
    if service == "s3":
        client.head_bucket = head_bucket
    return client


def test_root_index_html_is_uploaded_last(s3, tmp_path, mocker):
    """Route pages are index.html files too; only the ROOT one marks a complete upload."""
    archive = _write_archive(tmp_path / "a.zip", extra={"ui/pipelines/index.html": b"p", "ui/runs/index.html": b"r"})
    root, manifest = up.extract_archive(archive)
    bucket = "polyris-artifacts-order"
    s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION})
    upload_file = mocker.spy(s3, "upload_file")

    _, _, prefix = up.upload_artifacts(s3, bucket, root, manifest, owner=ACCOUNT)

    ui_keys = [c.args[2] for c in upload_file.call_args_list if c.args[2].startswith(prefix)]
    assert ui_keys[-1] == f"{prefix}/index.html"
    assert f"{prefix}/pipelines/index.html" in ui_keys[:-1]


def test_session_reads_aws_region(aws, monkeypatch):
    monkeypatch.delenv("AWS_DEFAULT_REGION")
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    assert up._session(None, None)[1] == "ap-south-1"
    assert up._session(None, "eu-west-1")[1] == "eu-west-1", "--region still wins"


def test_up_refuses_to_roll_back_a_newer_platform(fake_cfn, s3, tmp_path, capsys):
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="UPDATE_COMPLETE", Parameters=[],
                                         Description="Polyris platform v999.0.0")])
    archive = str(_write_archive(tmp_path / "a.zip"))

    assert up._run(up.run_up, _up_args(archive=archive)) == 1
    assert "roll it back" in capsys.readouterr().err
    assert "create_change_set" not in fake_cfn["cfn"].names()

    assert up._run(up.run_up, _up_args(archive=archive, allow_downgrade=True)) == 0


def test_up_never_puts_1_2_back_under_a_platform_that_runs_1_3(fake_cfn, s3, tmp_path, capsys):
    """The architect's review of TR-8 (A-15), the maintainer's choice: a pipeline deployed for 1.3 has no engine on
    1.2, so no flag rolls a 1.3 platform back below 1.3."""
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="UPDATE_COMPLETE", Parameters=[],
                                         Description="Polyris platform v1.3.0")])
    archive = str(_write_archive(tmp_path / "a.zip", version="1.2.3"))

    assert up._run(up.run_up, _up_args(archive=archive, allow_downgrade=True)) == 1
    assert "No flag allows this" in capsys.readouterr().err
    assert "create_change_set" not in fake_cfn["cfn"].names()


def test_up_never_finishes_someone_elses_pending_stack(fake_cfn, tmp_path, capsys):
    other = {"StackName": "other-app", "StackStatus": "REVIEW_IN_PROGRESS", "Tags": [],
             "_change_sets": [{"ChangeSetName": "their-tool-123"}]}
    fake_cfn["cfn"] = FakeCfn([other])
    assert up._run(up.run_up, _up_args(stack="other-app", archive=str(_write_archive(tmp_path / "a.zip")))) == 1
    assert "not a Polyris platform stack" in capsys.readouterr().err


def test_up_finishes_its_own_untagged_pending_stack(fake_cfn, s3, tmp_path):
    ours = {"StackName": "acme-dev", "StackStatus": "REVIEW_IN_PROGRESS", "Tags": [],
            "_change_sets": [{"ChangeSetName": "polyris-1731400000000"}]}
    fake_cfn["cfn"] = FakeCfn([ours])
    args = _up_args(namespace="acme", stage="dev", admin_email="", archive=str(_write_archive(tmp_path / "a.zip")))
    assert up._run(up.run_up, args) == 0
    assert fake_cfn["cfn"].last_change_set["ChangeSetType"] == "CREATE"


def test_dead_platform_stack_does_not_block_a_new_install(fake_cfn, capsys):
    """A failed acme-prod (ROLLBACK_COMPLETE, still tagged) is skipped by discovery."""
    fake_cfn["cfn"] = FakeCfn([_platform("acme-prod", StackStatus="ROLLBACK_COMPLETE")])
    assert up._run(up.run_up, _up_args()) == 1
    assert "new install" in capsys.readouterr().out  # then stops: no namespace without a terminal


def test_next_step_flags_repeat_the_target():
    args = _up_args(stack="acme-prod", profile="sandbox")
    args.region = "eu-west-1"
    assert up.next_step_flags(args, "acme-prod") == " --stack acme-prod --profile sandbox --region eu-west-1"
    plain = _up_args()
    plain.region = None
    assert up.next_step_flags(plain, "acme-dev") == ""


def test_down_survives_resources_without_physical_id(fake_cfn, s3, capsys):
    stack = _stack_with_buckets(s3, results_objects=0)
    stack["_resources"].append({"LogicalResourceId": "Broken", "ResourceType": "AWS::DynamoDB::Table"})
    fake_cfn["cfn"] = FakeCfn([stack])
    assert up._run(up.run_down, _down_args(yes=True)) == 0
    assert "1 DynamoDB tables" in capsys.readouterr().out


def test_down_finds_pipelines_through_the_registry(fake_cfn, s3, capsys):
    """Pipelines named by config.py (namespace ≠ platform's) are found via the registry,
    and destroyed pipelines (state machine gone) are not."""
    ddb = boto3.client("dynamodb", region_name=REGION)
    ddb.create_table(TableName="acme-dev-polyris-pipeline-registry", BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "pipeline_name", "KeyType": "HASH"}],
                     AttributeDefinitions=[{"AttributeName": "pipeline_name", "AttributeType": "S"}])
    sfn = boto3.client("stepfunctions", region_name=REGION)
    live = sfn.create_state_machine(name="other-ns-dev-polyris-orders", definition="{}",
                                    roleArn=f"arn:aws:iam::{ACCOUNT}:role/r")["stateMachineArn"]
    gone = f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:other-ns-dev-polyris-old"
    for name, arn in (("orders", live), ("old", gone)):
        ddb.put_item(TableName="acme-dev-polyris-pipeline-registry",
                     Item={"pipeline_name": {"S": name}, "sfn_arn": {"S": arn}})
    stack = _stack_with_buckets(s3, results_objects=0)
    stack["Outputs"] = [{"OutputKey": "PipelineRegistryTable", "OutputValue": "acme-dev-polyris-pipeline-registry"}]
    fake_cfn["cfn"] = FakeCfn([stack])

    assert up._run(up.run_down, _down_args()) == 1

    err = capsys.readouterr().err
    assert "1 pipeline(s) still deployed on this platform: orders" in err


# ── Second review round (PR #51) ─────────────────────────────────────────────


def _record_s3_params(client):
    """Capture (operation, params) of every S3 API call the client makes — the boto3 boundary.

    moto ignores ExpectedBucketOwner, so asserting it reached the request is the only way to
    pin the protection in tests.
    """
    calls = []

    def capture(params, model, **kw):
        calls.append((model.name, dict(params)))

    client.meta.events.register("provide-client-params.s3.*", capture)
    return calls


def test_every_artifacts_bucket_request_carries_expected_owner(s3, tmp_path):
    calls = _record_s3_params(s3)
    root, manifest = up.extract_archive(_write_archive(tmp_path / "a.zip"))
    bucket = artifacts_bucket_name(ACCOUNT, REGION)

    up.ensure_artifacts_bucket(s3, bucket, region=REGION, owner=ACCOUNT)
    up.upload_artifacts(s3, bucket, root, manifest, owner=ACCOUNT)
    up._upload_template(s3, bucket=bucket, region=REGION, version="1", template={}, owner=ACCOUNT)
    up.count_objects(s3, bucket, owner=ACCOUNT)
    up.empty_bucket(s3, bucket, owner=ACCOUNT)
    up.bucket_owner_status(s3, bucket, owner=ACCOUNT)

    ops = [(op, p) for op, p in calls if p.get("Bucket") == bucket and op != "CreateBucket"]
    assert {op for op, _ in ops} >= {"HeadBucket", "HeadObject", "PutObject", "PutPublicAccessBlock",
                                     "PutBucketEncryption", "PutBucketTagging", "ListObjectVersions",
                                     "DeleteObjects"}
    missing = [op for op, p in ops if p.get("ExpectedBucketOwner") != ACCOUNT]
    assert missing == [], f"S3 calls without ExpectedBucketOwner: {missing}"


def test_flagless_up_points_at_a_failed_platform_stack(fake_cfn, capsys):
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="ROLLBACK_COMPLETE")])
    assert up._run(up.run_up, _up_args()) == 1  # stops at the namespace prompt (no terminal)
    assert "polyris-down --stack acme-dev" in capsys.readouterr().out


@pytest.mark.parametrize("status", ["ROLLBACK_COMPLETE", "DELETE_FAILED"])
def test_named_dead_stack_points_at_polyris_down(fake_cfn, capsys, status):
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus=status)])
    assert up._run(up.run_up, _up_args(namespace="acme", stage="dev")) == 1
    err = capsys.readouterr().err
    assert status in err and "polyris-down --stack acme-dev" in err


def test_session_reads_polyris_region_first(aws, monkeypatch):
    monkeypatch.delenv("AWS_DEFAULT_REGION")
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    monkeypatch.setenv("POLYRIS_REGION", "sa-east-1")
    assert up._session(None, None)[1] == "sa-east-1"


def test_down_deletes_its_own_pending_stack(fake_cfn, capsys):
    pending = {"StackName": "acme-dev", "StackStatus": "REVIEW_IN_PROGRESS", "Tags": [],
               "_change_sets": [{"ChangeSetName": "polyris-1731400000000"}], "_resources": []}
    fake_cfn["cfn"] = FakeCfn([pending])
    assert up._run(up.run_down, _down_args(stack="acme-dev", yes=True)) == 0
    assert "delete_stack" in fake_cfn["cfn"].names()


def _registry(rows):
    ddb = boto3.client("dynamodb", region_name=REGION)
    ddb.create_table(TableName="reg", BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "pipeline_name", "KeyType": "HASH"}],
                     AttributeDefinitions=[{"AttributeName": "pipeline_name", "AttributeType": "S"}])
    for row in rows:
        ddb.put_item(TableName="reg", Item={k: {"S": v} for k, v in row.items()})


def test_registry_rows_without_arn_count_as_live_and_names_are_listed_once(fake_cfn, s3):
    _registry([{"pipeline_name": "pending"}, {"pipeline_name": "orders"}])
    stack = _platform("acme-dev", Parameters=[{"ParameterKey": "Namespace", "ParameterValue": "acme"},
                                              {"ParameterKey": "Stage", "ParameterValue": "dev"}],
                      Outputs=[{"OutputKey": "PipelineRegistryTable", "OutputValue": "reg"}])
    cfn = FakeCfn([stack, {"StackName": "acme-dev-polyris-orders", "Tags": []}])
    session = boto3.Session(region_name=REGION)
    assert up.pipelines_on_platform(session, cfn, stack, "acme-dev") == ["orders", "pending"]


def test_registry_skips_state_machines_being_deleted(fake_cfn, s3, mocker):
    _registry([{"pipeline_name": "going", "sfn_arn": f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:going"}])
    stack = _platform("acme-dev", Outputs=[{"OutputKey": "PipelineRegistryTable", "OutputValue": "reg"}])
    session = boto3.Session(region_name=REGION)
    real_client = session.client

    def client(name, *a, **k):
        c = real_client(name, *a, **k)
        if name == "stepfunctions":
            mocker.patch.object(c, "describe_state_machine", return_value={"status": "DELETING"})
        return c

    mocker.patch.object(session, "client", side_effect=client)
    assert up.pipelines_on_platform(session, FakeCfn([stack]), stack, "acme-dev") == []


# ── The move from 1.2 to 1.3 (ADR #137, *Migration and upgrade*) ─────────────
#
# polyris/upgrade.py's own tests cover each check; these cover what polyris-up does with them.

TOKENS = "acme-dev-polyris-pipeline-tokens"
REGISTRY = "acme-dev-polyris-pipeline-registry"
SPECS = "acme-dev-polyris-pipeline-specs"
ENGINE_1_2 = ("dependency-wrapper", "registration-helper", "failure-handler", "pause-waiter",
              "dep-run-task-helper", "bulk-backfill")
MACHINES = f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:"
#: What the stack of acme-dev names once it runs 1.3, beside its tasks table.
OUTPUTS_1_3 = [*ROWS_OUTPUTS, {"OutputKey": "PipelineRegistryTable", "OutputValue": REGISTRY},
               {"OutputKey": "PipelineSpecsTable", "OutputValue": SPECS},
               {"OutputKey": "ConsoleUiUrl", "OutputValue": "https://d123.cloudfront.net"}]
#: What the update to this polyris changes of the stack, and the cleanup it then passes through.
UPGRADED = {"updated": {"Description": f"Polyris platform v{__version__}",
                        "Outputs": [{"OutputKey": "PipelineTokensTable", "OutputValue": TOKENS}, *OUTPUTS_1_3]},
            "progress": ["UPDATE_COMPLETE_CLEANUP_IN_PROGRESS"]}


def _deployed(version: str | None = "1.2.3", *, outputs: List[Dict[str, str]] | None = None) -> Dict[str, Any]:
    """The stack of the platform acme-dev at ``version``; None is a stack that `sam deploy` made,
    which names no version. ``outputs`` are those it has beside its tasks table."""
    described = {"Description": f"Polyris platform v{version}"} if version else {}
    return _platform("acme-dev", StackStatus="UPDATE_COMPLETE", **described,
                     Parameters=[{"ParameterKey": "Namespace", "ParameterValue": "acme"},
                                 {"ParameterKey": "Stage", "ParameterValue": "dev"}],
                     Outputs=[{"OutputKey": "PipelineTokensTable", "OutputValue": TOKENS},
                              {"OutputKey": "PipelineRegistryTable", "OutputValue": REGISTRY}, *(outputs or [])])


@pytest.fixture
def platform_1_2(fake_cfn) -> FakeStates:
    """A 1.2 platform that is ready for 1.3: its adoption settled three days ago, and nothing runs.

    Returns its Step Functions, where a test starts what it needs running. ``pipeline-specs`` is
    there as the 1.3 update creates it: what started before this moment is 1.2's.
    """
    finished = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    ddb = boto3.client("dynamodb", region_name=REGION)
    ddb.create_table(TableName=TOKENS, BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "execution_name", "KeyType": "HASH"}],
                     AttributeDefinitions=[{"AttributeName": "execution_name", "AttributeType": "S"},
                                           {"AttributeName": "start_day", "AttributeType": "S"}],
                     # The index that the 1.3 update adds: it is there, built, once the update is through.
                     GlobalSecondaryIndexes=[{"IndexName": "run-start-index",
                                              "KeySchema": [{"AttributeName": "start_day", "KeyType": "HASH"}],
                                              "Projection": {"ProjectionType": "ALL"}}])
    for table, key in ((REGISTRY, "pipeline_name"), (SPECS, "pk")):
        ddb.create_table(TableName=table, BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                         AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}])
    ddb.put_item(TableName=TOKENS, Item={
        "execution_name": {"S": "_engine_health"}, "adoption_generation": {"N": "1"},
        "adopt_tasks_cursor": {"S": ""}, "adopt_tasks_until": {"S": finished},
        "adopt_facts_cursor": {"S": ""}, "adopt_facts_until": {"S": finished}})
    states = FakeStates()
    for name in ENGINE_1_2:
        states.machine(f"acme-dev-polyris-{name}")
    fake_cfn["cfn"] = FakeCfn([_deployed()])
    fake_cfn["stepfunctions"] = states.client
    return states


def _a_run_in_flight(states: FakeStates, machine: str = "dependency-wrapper") -> None:
    states.run(f"acme-dev-polyris-{machine}", "orders-run-2026-11-23-ab12cd34",
               started=datetime.now(timezone.utc) - timedelta(hours=1))


def _pipeline(states: FakeStates, name: str, *, registered: bool = True, redeployed: bool = False) -> None:
    """A deployed pipeline: its state machine and its registry row. The row has the ARN only once the
    pipeline ``registered``, and the format only once 1.3's polyris-deploy ``redeployed`` it."""
    states.machine(f"acme-dev-polyris-{name}")
    arn = {"sfn_arn": {"S": f"{MACHINES}acme-dev-polyris-{name}"}} if registered else {}
    deployed_format = {"format_version": {"N": "1"}} if redeployed else {}
    boto3.client("dynamodb", region_name=REGION).put_item(
        TableName=REGISTRY, Item={"pipeline_name": {"S": name}, **arn, **deployed_format})


def _a_1_2_run(states: FakeStates, pipeline: str = "orders") -> None:
    """A run of ``pipeline`` that started an hour before the 1.3 update: its execution, a wrapper and its work."""
    started = datetime.now(timezone.utc) - timedelta(hours=1)
    states.run(f"acme-dev-polyris-{pipeline}", f"{pipeline}-run-2026-11-23-ab12cd34", started=started)
    states.run("acme-dev-polyris-dependency-wrapper", f"{pipeline}-extract-wrapper", started=started)
    states.run("acme-dev-polyris-dep-run-task-helper", f"{pipeline}-extract-work", started=started)


def test_up_upgrades_a_1_2_platform_that_is_ready(fake_cfn, platform_1_2, s3, tmp_path, capsys):
    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 0

    assert "checking the platform before its upgrade to 1.3" in capsys.readouterr().out
    assert "execute_change_set" in fake_cfn["cfn"].names()


def test_up_refuses_a_1_2_platform_with_a_run_in_flight_before_it_changes_anything(
        fake_cfn, platform_1_2, s3, tmp_path, capsys):
    _a_run_in_flight(platform_1_2)

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 1

    err = capsys.readouterr().err
    assert "A run is in flight on acme-dev" in err and "--accept-stopped-runs" in err
    assert "create_change_set" not in fake_cfn["cfn"].names()
    assert up.bucket_owner_status(s3, artifacts_bucket_name(ACCOUNT, REGION), owner=ACCOUNT) == "missing"
    assert platform_1_2.stopped == []


def test_up_with_the_flag_upgrades_a_platform_with_a_run_in_flight(fake_cfn, platform_1_2, s3, tmp_path, capsys):
    _a_run_in_flight(platform_1_2)
    args = _up_args(archive=str(_write_archive(tmp_path / "a.zip")), accept_stopped_runs=True)

    assert up._run(up.run_up, args) == 0

    assert "--accept-stopped-runs: 1.2's adoption and the runs in flight were not checked." in capsys.readouterr().out
    assert "execute_change_set" in fake_cfn["cfn"].names()


def test_up_never_upgrades_a_platform_older_than_1_2(fake_cfn, platform_1_2, s3, tmp_path, capsys):
    fake_cfn["cfn"] = FakeCfn([_deployed("1.1.4")])
    args = _up_args(archive=str(_write_archive(tmp_path / "a.zip")), accept_stopped_runs=True, yes=True)

    assert up._run(up.run_up, args) == 1

    err = capsys.readouterr().err
    assert "acme-dev runs v1.1.4, and 1.3 upgrades a platform from 1.2 only" in err
    assert "--accept-stopped-runs does not cover this check" in err
    assert "create_change_set" not in fake_cfn["cfn"].names()


def test_up_tells_a_sam_deployed_1_2_from_1_1_4_by_the_health_row(fake_cfn, platform_1_2, s3, tmp_path, capsys):
    """`sam deploy` stamps no version into the stack: the row only 1.2 writes tells."""
    fake_cfn["cfn"] = FakeCfn([_deployed(None)])
    args = _up_args(archive=str(_write_archive(tmp_path / "a.zip")))
    assert up._run(up.run_up, args) == 0

    boto3.client("dynamodb", region_name=REGION).delete_item(
        TableName=TOKENS, Key={"execution_name": {"S": "_engine_health"}})
    fake_cfn["cfn"] = FakeCfn([_deployed(None)])
    assert up._run(up.run_up, args) == 1
    assert "acme-dev looks older than 1.2.0" in capsys.readouterr().err


@pytest.mark.parametrize("stack", [
    _deployed("1.3.0rc1"),                               # a pre-release of 1.3.0 runs 1.3's engine
    _deployed("1.3.0"),
    _deployed(None, outputs=ROWS_OUTPUTS),               # from `sam deploy`: the output 1.3 adds tells
], ids=["1.3.0rc1", "1.3.0", "sam deploy of 1.3"])
def test_up_makes_no_such_checks_on_a_platform_that_runs_1_3(fake_cfn, platform_1_2, s3, tmp_path, capsys, stack):
    """In 1.3 a run_task execution is running almost all the time: a check for runs in flight
    would refuse every patch release."""
    fake_cfn["cfn"] = FakeCfn([stack])
    _a_run_in_flight(platform_1_2, "dep-run-task-helper")
    args = _up_args(archive=str(_write_archive(tmp_path / "a.zip", version="1.3.1")), yes=True)

    assert up._run(up.run_up, args) == 0

    assert "checking the platform" not in capsys.readouterr().out
    assert "execute_change_set" in fake_cfn["cfn"].names()


def test_up_makes_no_such_checks_for_a_release_that_keeps_1_2s_engine(fake_cfn, platform_1_2, s3, tmp_path, capsys):
    """An --archive of a 1.2 build deletes nothing a run needs."""
    _a_run_in_flight(platform_1_2)
    args = _up_args(archive=str(_write_archive(tmp_path / "a.zip", version="1.2.4")))

    assert up._run(up.run_up, args) == 0

    assert "checking the platform" not in capsys.readouterr().out


def test_up_stops_what_1_2_left_running_as_the_cleanup_begins(fake_cfn, platform_1_2, s3, tmp_path, capsys):
    """The cleanup that deletes 1.2's state machines waits for their executions (TR-0 A2), so they are
    stopped before polyris-up waits for the stack to settle; the runs to start again are printed."""
    _pipeline(platform_1_2, "orders")
    _a_1_2_run(platform_1_2)
    fake_cfn["cfn"] = cfn = FakeCfn([_deployed()], **UPGRADED)
    stopped_when_the_wait_began: List[str] = []
    cfn.on_wait = lambda waiter: stopped_when_the_wait_began.extend(platform_1_2.stopped)
    updated_when_stopped: List[bool] = []
    platform_1_2.on_stop = lambda name: updated_when_stopped.append("execute_change_set" in cfn.names())
    args = _up_args(archive=str(_write_archive(tmp_path / "a.zip")), accept_stopped_runs=True)

    assert up._run(up.run_up, args) == 0

    assert stopped_when_the_wait_began == ["orders-run-2026-11-23-ab12cd34", "orders-extract-wrapper",
                                           "orders-extract-work"]
    assert updated_when_stopped == [True] * 3, "nothing is stopped before the update has succeeded"
    assert platform_1_2.running() == []
    out = capsys.readouterr().out
    assert "Stopped what 1.2 left running: 1 run(s), and 2 execution(s) of the engine's state machines." in out
    assert "acme-dev-polyris-orders  orders-run-2026-11-23-ab12cd34  (started " in out
    assert "start these again once their pipelines are redeployed" in out


def test_up_keeps_what_started_after_the_1_3_update(fake_cfn, platform_1_2, s3, tmp_path, capsys):
    """An ordinary update of a platform that runs 1.3 stops none of its runs."""
    _pipeline(platform_1_2, "orders")
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    platform_1_2.run("acme-dev-polyris-orders", "orders-run-2026-11-24-0f0f0f0f", started=later)
    platform_1_2.run("acme-dev-polyris-dep-run-task-helper", "extract-work", started=later)
    fake_cfn["cfn"] = FakeCfn([_deployed("1.3.0a1", outputs=OUTPUTS_1_3)], **UPGRADED)

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 0

    listed = {params["stateMachineArn"] for name, params in platform_1_2.calls if name == "ListExecutions"}
    assert {f"{MACHINES}acme-dev-polyris-orders", f"{MACHINES}acme-dev-polyris-dep-run-task-helper"} <= listed
    assert platform_1_2.stopped == []
    assert "Stopped what 1.2 left running" not in capsys.readouterr().out


def test_a_polyris_up_stops_what_is_left_of_1_2_while_its_state_machines_are_there(fake_cfn, platform_1_2, s3,
                                                                                    tmp_path, capsys):
    """Also one with nothing to change: an upgrade interrupted after its update is finished by the next run, as
    long as a state machine of 1.2 is left (upgrade.left_by_1_2)."""
    _pipeline(platform_1_2, "orders")
    _a_1_2_run(platform_1_2)
    fake_cfn["cfn"] = FakeCfn([_deployed(__version__, outputs=OUTPUTS_1_3)], change_status="FAILED",
                              status_reason="didn't contain changes")

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 0

    out = capsys.readouterr().out
    assert "already up to date" in out and "Stopped what 1.2 left running: 1 run(s)" in out
    assert platform_1_2.running() == []


def test_a_platform_that_finished_its_upgrade_is_asked_for_no_execution(fake_cfn, platform_1_2, s3, tmp_path,
                                                                       capsys):
    """The architect's review of TR-8 (B-1): every polyris-up on 1.3 listed and stopped executions, which the
    install's permissions do not grant, and failed after a good update with a message about 1.2. Once none of
    1.2's state machines is left, it only sees so (DescribeStateMachine)."""
    for name in ENGINE_1_2[:4]:                     # REMOVED_STANDARD_MACHINES: the upgrade's cleanup deleted them
        platform_1_2.machines.pop(f"{MACHINES}acme-dev-polyris-{name}")
    _pipeline(platform_1_2, "orders", redeployed=True)
    fake_cfn["cfn"] = FakeCfn([_deployed(__version__, outputs=OUTPUTS_1_3)], change_status="FAILED",
                              status_reason="didn't contain changes")

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 0

    assert "already up to date" in capsys.readouterr().out
    assert not {"ListExecutions", "StopExecution", "ListStateMachines"} & set(platform_1_2.operations())


def test_up_finishes_an_upgrade_that_was_interrupted_in_its_cleanup(fake_cfn, platform_1_2, s3, tmp_path, capsys):
    """The stack is updated and its cleanup waits for a wrapper: polyris-up stops it and waits, where
    it used to say the stack is busy."""
    platform_1_2.run("acme-dev-polyris-dependency-wrapper", "orders-extract-wrapper",
                     started=datetime.now(timezone.utc) - timedelta(hours=1))
    stack = _deployed(__version__, outputs=OUTPUTS_1_3)
    stack["StackStatus"] = "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS"
    fake_cfn["cfn"] = cfn = FakeCfn([stack], change_status="FAILED", status_reason="didn't contain changes")
    cfn.on_wait = lambda waiter: cfn.calls.append(("stopped", list(platform_1_2.stopped)))

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 0

    assert ("stopped", ["orders-extract-wrapper"]) in cfn.calls, "before it waits for the stack"
    assert cfn.names().index("wait") < cfn.names().index("create_change_set")
    out = capsys.readouterr().out
    assert "acme-dev is still cleaning up after its last update" in out and "already up to date" in out


def test_up_stops_nothing_for_a_stack_that_is_no_platform(fake_cfn, platform_1_2, capsys):
    """`--stack my-app` on a stack that only looks like one in its cleanup: refused before any stop."""
    platform_1_2.run("acme-dev-polyris-dependency-wrapper", "someone-elses",
                     started=datetime.now(timezone.utc) - timedelta(hours=1))
    fake_cfn["cfn"] = FakeCfn([{"StackName": "acme-dev", "StackStatus": "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
                                "Tags": [], "Description": "Polyris platform v1.3.0",
                                "Parameters": [{"ParameterKey": "TableName", "ParameterValue": "invoices"}]}])

    assert up._run(up.run_up, _up_args(stack="acme-dev", yes=True)) == 1

    assert "not a Polyris platform stack" in capsys.readouterr().err
    assert platform_1_2.calls == []


def test_up_still_refuses_a_stack_that_another_update_keeps_busy(fake_cfn, platform_1_2, capsys):
    """A cleanup on a platform below 1.3 has nothing of 1.2 to stop: it is some other update's."""
    stack = _deployed()
    stack["StackStatus"] = "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS"
    fake_cfn["cfn"] = FakeCfn([stack])

    assert up._run(up.run_up, _up_args()) == 1

    assert "busy (UPDATE_COMPLETE_CLEANUP_IN_PROGRESS)" in capsys.readouterr().err


def test_up_says_what_it_could_not_stop_and_how_to_go_on(fake_cfn, platform_1_2, s3, tmp_path, capsys):
    _pipeline(platform_1_2, "orders")
    _a_1_2_run(platform_1_2)
    platform_1_2.denied.add("StopExecution")
    fake_cfn["cfn"] = FakeCfn([_deployed()], **UPGRADED)
    args = _up_args(archive=str(_write_archive(tmp_path / "a.zip")), accept_stopped_runs=True)

    assert up._run(up.run_up, args) == 1

    err = capsys.readouterr().err
    assert "acme-dev is updated, but polyris-up could not stop what 1.2 left running" in err
    assert "AccessDeniedException" in err and "run polyris-up again" in err


@pytest.mark.parametrize("flag", [False, True])
def test_up_is_refused_before_the_update_when_it_may_not_read_the_registry(
        fake_cfn, platform_1_2, s3, tmp_path, capsys, flag):
    """The stop reads the registry after the update: better refused now, while nothing has changed."""
    fake_cfn["dynamodb"] = ddb = boto3.client("dynamodb", region_name=REGION)
    refuse(ddb, "Scan")
    args = _up_args(archive=str(_write_archive(tmp_path / "a.zip")), accept_stopped_runs=flag)

    assert up._run(up.run_up, args) == 1

    assert f"polyris-up may not call dynamodb:Scan on {REGISTRY}" in capsys.readouterr().err
    assert "create_change_set" not in fake_cfn["cfn"].names()


def test_up_finds_a_pipeline_the_registry_does_not_name_by_its_state_machines_name(
        fake_cfn, platform_1_2, s3, tmp_path):
    """A pipeline whose registration never finished, and one that has only its stack."""
    _pipeline(platform_1_2, "pending", registered=False)
    platform_1_2.machine("acme-dev-polyris-unregistered")
    platform_1_2.machine("acme-prod-polyris-pending")                     # another platform's pipeline
    for machine in ("acme-dev-polyris-pending", "acme-dev-polyris-unregistered", "acme-prod-polyris-pending"):
        platform_1_2.run(machine, f"a-run-of-{machine}", started=datetime.now(timezone.utc) - timedelta(hours=1))
    fake_cfn["cfn"] = FakeCfn([_deployed(), {"StackName": "acme-dev-polyris-unregistered", "Tags": []}], **UPGRADED)
    args = _up_args(archive=str(_write_archive(tmp_path / "a.zip")), accept_stopped_runs=True)

    assert up._run(up.run_up, args) == 0

    assert platform_1_2.running() == ["a-run-of-acme-prod-polyris-pending"]
    assert "ListStateMachines" in platform_1_2.operations()


def test_up_stops_nothing_for_a_release_that_keeps_1_2s_engine(fake_cfn, platform_1_2, s3, tmp_path):
    _pipeline(platform_1_2, "orders")
    _a_1_2_run(platform_1_2)
    fake_cfn["cfn"] = FakeCfn([_deployed()], progress=["UPDATE_COMPLETE_CLEANUP_IN_PROGRESS"])
    args = _up_args(archive=str(_write_archive(tmp_path / "a.zip", version="1.2.4")))

    assert up._run(up.run_up, args) == 0

    assert platform_1_2.stopped == []


def test_up_lists_the_pipelines_that_still_need_a_redeploy_with_the_command_for_each(
        fake_cfn, platform_1_2, s3, tmp_path, capsys):
    """Until its redeploy a 1.2 pipeline's runs fail at once: the list says which, and how."""
    _pipeline(platform_1_2, "orders")
    _pipeline(platform_1_2, "customers", redeployed=True)                  # from CI, while the upgrade ran
    _pipeline(platform_1_2, "pending", registered=False)
    boto3.client("dynamodb", region_name=REGION).put_item(TableName=REGISTRY, Item={      # destroyed since
        "pipeline_name": {"S": "retired"}, "sfn_arn": {"S": f"{MACHINES}acme-dev-polyris-retired"}})
    fake_cfn["cfn"] = FakeCfn([_deployed(), {"StackName": "acme-dev-polyris-unregistered", "Tags": []}], **UPGRADED)
    args = _up_args(archive=str(_write_archive(tmp_path / "a.zip")), stack="acme-dev")

    assert up._run(up.run_up, args) == 0

    out = capsys.readouterr().out
    assert "3 pipeline(s) were deployed with an older pipeline format. Their runs fail at once" in out
    listed = [line.strip() for line in out.splitlines() if line.strip().startswith("polyris-deploy --select")]
    assert listed == [f"polyris-deploy --select {name} --stack acme-dev --region {REGION}"
                      for name in ("orders", "pending", "unregistered")]
    assert "polyris-deploy --all" in out
    assert "a first pipeline" not in out, "the redeploy comes next"


def test_up_lists_none_once_every_pipeline_is_redeployed(fake_cfn, platform_1_2, s3, tmp_path, capsys):
    _pipeline(platform_1_2, "orders", redeployed=True)
    fake_cfn["cfn"] = FakeCfn([_deployed("1.3.0a1", outputs=OUTPUTS_1_3)], **UPGRADED)

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 0

    out = capsys.readouterr().out
    assert "older pipeline format" not in out and "polyris-deploy --select" not in out
    # An update said "Next — a first pipeline" to a platform of 22 pipelines (1.3.0 on dev)
    assert "a first pipeline" not in out, "an update: the pipelines run as before"


def test_up_with_nothing_to_change_still_lists_the_pipelines_to_redeploy(fake_cfn, platform_1_2, s3, tmp_path, capsys):
    _pipeline(platform_1_2, "orders")
    fake_cfn["cfn"] = FakeCfn([_deployed(__version__, outputs=OUTPUTS_1_3)], change_status="FAILED",
                              status_reason="didn't contain changes")

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 0

    out = capsys.readouterr().out
    assert "already up to date" in out and f"polyris-deploy --select orders --region {REGION}" in out


def test_up_waits_until_dynamodb_has_built_the_index_that_1_3_adds(fake_cfn, platform_1_2, s3, tmp_path, capsys):
    """CloudFormation reports the table updated while the index is still being built (TR-0 A3)."""
    building = {"Table": {"TableName": TOKENS, "GlobalSecondaryIndexes": [
        {"IndexName": "run-start-index", "IndexStatus": "CREATING", "Backfilling": True}]}}
    fake_cfn["dynamodb"] = ddb = boto3.client("dynamodb", region_name=REGION)
    answering(ddb, "DescribeTable", building, building, where={"TableName": TOKENS})
    fake_cfn["cfn"] = FakeCfn([_deployed()], **UPGRADED)

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 0

    out = capsys.readouterr().out
    assert out.index("is up: acme-dev") < out.index(f"waiting for DynamoDB to build run-start-index on {TOKENS}")
    assert "run-start-index is active" in out


def test_up_makes_none_of_this_of_a_first_install(fake_cfn, s3, tmp_path, capsys):
    """A new platform has nothing of 1.2, its index is built with its table, and it has no pipeline yet."""
    fake_cfn["stepfunctions"] = (states := FakeStates()).client
    args = _up_args(namespace="acme", stage="dev", admin_email="", archive=str(_write_archive(tmp_path / "a.zip")))

    assert up._run(up.run_up, args) == 0

    assert states.calls == []
    assert "a first pipeline" in capsys.readouterr().out

