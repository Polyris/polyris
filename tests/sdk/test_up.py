"""Tests for polyris-up / polyris-down (polyris/up.py, ADR #131).

Boundaries (Principle #14): S3, STS, IAM and DynamoDB run on moto; the release
download is mocked at ``urllib.request.urlopen``. CloudFormation is a small
fake client, because moto cannot expand the SAM transform or run change sets
— the fake records every call so the tests can assert the exact request
polyris-up sends, and reads a change set's template from moto's S3.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from fake_states import FakeStates
from polyris import __version__
from polyris import up
from polyris.platform import (
    PLATFORM_TAG_KEY,
    PLATFORM_TAG_VALUE,
    Manifest,
    artifacts_bucket_name,
    release_asset_name,
)

ACCOUNT = "123456789012"  # moto's default account
REGION = "eu-central-1"
#: An older platform that polyris-up updates: 1.3 or newer, by its version or, for a stack that names none,
#: by the output 1.3 adds.
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
        #: The stacks that import an export, by its name (ListImports).
        self.imports: Dict[str, List[str]] = {}
        #: What DescribeStackResources answers for a physical id; no entry: no stack holds it.
        self.resources: Dict[str, List[Dict[str, Any]]] = {}
        #: Called between a change set's creation and the read of its template (a writer of the bucket).
        self.on_create = None

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
                if name == "list_imports":
                    if not fake.imports.get(kw["ExportName"]):
                        raise ClientError({"Error": {"Code": "ValidationError", "Message": (
                            f"Export '{kw['ExportName']}' is not imported by any stack.")}}, "ListImports")
                    return [{"Imports": fake.imports[kw["ExportName"]]}]
                return [{"StackResourceSummaries": fake.stacks[kw["StackName"]].get("_resources", [])}]
        return _P()

    def describe_stack_events(self, StackName):
        return {"StackEvents": []}

    def create_change_set(self, **kw):
        self.calls.append(("create_change_set", kw))
        self.last_change_set = kw
        if self.on_create:
            self.on_create(kw)

    def get_template(self, StackName, ChangeSetName, TemplateStage):
        """The change set's template as CloudFormation read it: the object behind its TemplateURL, now."""
        self.calls.append(("get_template", {"StackName": StackName, "TemplateStage": TemplateStage}))
        bucket, key = _s3_location(self.last_change_set["TemplateURL"])
        body = boto3.client("s3", region_name=REGION).get_object(Bucket=bucket, Key=key)["Body"].read().decode()
        return {"TemplateBody": json.loads(body)}

    def describe_stack_resources(self, PhysicalResourceId):
        if PhysicalResourceId not in self.resources:
            raise ClientError({"Error": {"Code": "ValidationError",
                                         "Message": f"Stack for {PhysicalResourceId} does not exist"}},
                              "DescribeStackResources")
        return {"StackResources": self.resources[PhysicalResourceId]}

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


def _s3_location(url: str) -> Tuple[str, str]:
    """(bucket, key) of a virtual-hosted S3 URL, as polyris-up builds it."""
    host, key = url.split("://", 1)[1].split("/", 1)
    return host.split(".s3.", 1)[0], key


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


def test_upload_artifacts_is_content_addressed_and_uploads_every_time(s3, tmp_path, capsys):
    """An object found under a key is not known to be the one polyris-up put there (ADR #142 §8)."""
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
    assert second == "artifacts: 4 uploaded"
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
    upload_file = mocker.spy(s3, "upload_file")

    up.upload_artifacts(s3, bucket, root, manifest, owner=ACCOUNT)

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


def test_session_reads_aws_region(aws, monkeypatch):
    monkeypatch.delenv("AWS_DEFAULT_REGION")
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    assert up._session(None, None)[1] == "ap-south-1"
    assert up._session(None, "eu-west-1")[1] == "eu-west-1", "--region still wins"


def test_up_refuses_to_roll_back_a_newer_platform(fake_cfn, s3, tmp_path, capsys):
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="UPDATE_COMPLETE", Parameters=[],
                                         Description="Polyris platform v1.3.99", Outputs=ROWS_OUTPUTS)])
    archive = str(_write_archive(tmp_path / "a.zip"))

    assert up._run(up.run_up, _up_args(archive=archive)) == 1
    assert "roll it back" in capsys.readouterr().err
    assert "create_change_set" not in fake_cfn["cfn"].names()

    assert up._run(up.run_up, _up_args(archive=archive, allow_downgrade=True)) == 0


def test_up_installs_no_release_below_1_3(fake_cfn, s3, tmp_path, capsys):
    """1.2 support ends with 1.4 (ADR #137, *Migration and upgrade*): a pipeline deployed for 1.3 has no engine on 1.2."""
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="UPDATE_COMPLETE", Parameters=[],
                                         Description="Polyris platform v1.3.0")])
    archive = str(_write_archive(tmp_path / "a.zip", version="1.2.3"))

    assert up._run(up.run_up, _up_args(archive=archive, allow_downgrade=True)) == 1
    assert "installs 1.3 or newer only" in capsys.readouterr().err
    assert "create_change_set" not in fake_cfn["cfn"].names()


def test_up_never_takes_a_platform_below_1_4_even_with_the_flag(fake_cfn, s3, tmp_path, capsys):
    """ADR #142 §4: its pipelines schedule with the schedule role, which 1.3 does not have."""
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="UPDATE_COMPLETE", Parameters=[],
                                         Description="Polyris platform v1.4.0")])
    archive = str(_write_archive(tmp_path / "a.zip", version="1.3.2"))

    assert up._run(up.run_up, _up_args(archive=archive, allow_downgrade=True)) == 1
    err = capsys.readouterr().err
    assert "no platform goes back below 1.4" in err and "No flag allows this" in err
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
    assert {op for op, _ in ops} >= {"HeadBucket", "PutObject", "PutPublicAccessBlock",
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


# ── The upgrade to 1.4 (ADR #142 §8) ──────────────────────────────────

REGISTRY = "acme-dev-polyris-pipeline-registry"
SPECS = "acme-dev-polyris-pipeline-specs"
MACHINES = f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:"
#: What the stack of acme-dev names on 1.3.
OUTPUTS_1_3 = [*ROWS_OUTPUTS, {"OutputKey": "PipelineRegistryTable", "OutputValue": REGISTRY},
               {"OutputKey": "PipelineSpecsTable", "OutputValue": SPECS},
               {"OutputKey": "ConsoleUiUrl", "OutputValue": "https://d123.cloudfront.net"}]
POLICY = f"arn:aws:iam::{ACCOUNT}:policy/acme-dev-polyris-deploy-policy"
ORCHESTRATION = f"arn:aws:iam::{ACCOUNT}:role/acme-dev-polyris-orchestration-role"
#: What the update to 1.4 changes of the stack.
UPDATED_1_4 = {"updated": {"Description": "Polyris platform v1.4.0",
                           "Outputs": [*OUTPUTS_1_3, {"OutputKey": "PipelineDeployPolicyArn", "OutputValue": POLICY},
                                       {"OutputKey": "OrchestrationRoleArn", "OutputValue": ORCHESTRATION}]}}


def _deployed(version: str | None = "1.3.2", *, outputs: List[Dict[str, str]] | None = None) -> Dict[str, Any]:
    """The stack of the platform acme-dev at ``version``; None is a stack that `sam deploy` made, which names
    no version."""
    described = {"Description": f"Polyris platform v{version}"} if version else {}
    return _platform("acme-dev", StackStatus="UPDATE_COMPLETE", **described,
                     Parameters=[{"ParameterKey": "Namespace", "ParameterValue": "acme"},
                                 {"ParameterKey": "Stage", "ParameterValue": "dev"}],
                     Outputs=OUTPUTS_1_3 if outputs is None else outputs)


@pytest.fixture
def platform_1_3(fake_cfn) -> FakeStates:
    """A 1.3 platform with its registry and specs tables; returns its Step Functions."""
    ddb = boto3.client("dynamodb", region_name=REGION)
    ddb.create_table(TableName=REGISTRY, BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "pipeline_name", "KeyType": "HASH"}],
                     AttributeDefinitions=[{"AttributeName": "pipeline_name", "AttributeType": "S"}])
    ddb.create_table(TableName=SPECS, BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
                     AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                           {"AttributeName": "sk", "AttributeType": "S"}])
    states = FakeStates()
    fake_cfn["cfn"] = FakeCfn([_deployed()])
    fake_cfn["stepfunctions"] = states.client
    return states


def _pipeline(states: FakeStates, name: str, *, role: str = "same", redeployed: bool = True,
              specs: bool = True) -> None:
    """A deployed pipeline: its state machine, its registry row and, with ``specs``, its current spec version,
    whose one task runs as ``role``. The row has the format only once 1.3's polyris-deploy ``redeployed`` it."""
    from polyris import DAG, task
    from polyris.coordination import spec_store
    from polyris.generators import build_spec_version

    states.machine(f"acme-dev-polyris-{name}")
    with DAG(name, schedule=None) as dag:
        @task.sfn(arn=f"{MACHINES}work", role=role)
        def load():
            pass
        load()
    ddb = boto3.client("dynamodb", region_name=REGION)
    version = build_spec_version(dag)
    item: Dict[str, Any] = {"pipeline_name": {"S": name}, "sfn_arn": {"S": f"{MACHINES}acme-dev-polyris-{name}"}}
    if specs:
        spec_store.write(ddb, SPECS, version)
        item.update({"spec_id": {"S": version.spec_id}, "task_count": {"N": str(version.task_count)}})
    if redeployed:
        item["format_version"] = {"N": "1"}
    ddb.put_item(TableName=REGISTRY, Item=item)


#: The scheduler role of the pipeline ``orders`` of the platform ``acme``/``dev``, as polyris-deploy 1.3.2 rendered
#: it (``deploy._generate_cfn_template`` at tag ``v1.3.2``, frozen): the form polyris-up must recognise.
SCHEDULER_ROLE_1_3 = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "scheduler_role_v1_3_2.json")
                                .read_text())


def _scheduler_role(name: str = "", *, policy: Dict[str, Any] | None = None, attach: str | None = None) -> str:
    """A role named like a pipeline's scheduler role, as CloudFormation makes it from 1.3.2's template unless
    changed; its ARN."""
    iam = boto3.client("iam", region_name=REGION)
    rendered = SCHEDULER_ROLE_1_3["Properties"]
    name = name or rendered["RoleName"]
    arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(rendered["AssumeRolePolicyDocument"]),
                          Tags=rendered["Tags"])["Role"]["Arn"]
    (inline,) = rendered["Policies"]
    # CloudFormation resolves the Ref to the stack's state machine.
    start = policy or json.loads(json.dumps(inline["PolicyDocument"]).replace(
        json.dumps({"Ref": "PipelineStateMachine"}), json.dumps(f"{MACHINES}acme-dev-polyris-orders")))
    iam.put_role_policy(RoleName=name, PolicyName=inline["PolicyName"], PolicyDocument=json.dumps(start))
    if attach:
        admin = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}
        policy_arn = iam.create_policy(PolicyName=attach, PolicyDocument=json.dumps(admin))["Policy"]["Arn"]
        iam.attach_role_policy(RoleName=name, PolicyArn=policy_arn)
    return arn


def _made_by_the_pipeline_stack(cfn: FakeCfn, name: str = "acme-dev-polyris-orders-scheduler-role") -> None:
    stack = "acme-dev-polyris-orders"
    cfn.resources[name] = [
        {"StackName": stack, "LogicalResourceId": "PipelineSchedulerRole", "PhysicalResourceId": name},
        {"StackName": stack, "LogicalResourceId": "PipelineStateMachine",
         "PhysicalResourceId": f"{MACHINES}acme-dev-polyris-orders"}]


def _err(capsys) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


@pytest.mark.parametrize("stack", [_deployed("1.2.3"), _deployed(None, outputs=[])])
def test_up_refuses_a_platform_below_1_3_before_it_changes_anything(fake_cfn, s3, tmp_path, capsys, stack):
    fake_cfn["cfn"] = FakeCfn([stack])

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 1

    err = _err(capsys)
    assert "upgrades a platform from 1.3 only" in err and "Upgrade it to 1.3 first" in err
    assert "create_change_set" not in fake_cfn["cfn"].names()
    assert artifacts_bucket_name(ACCOUNT, REGION) not in [b["Name"] for b in s3.list_buckets()["Buckets"]]


def test_up_updates_a_sam_deployed_1_3_platform(fake_cfn, platform_1_3, s3, tmp_path):
    fake_cfn["cfn"] = FakeCfn([_deployed(None)])
    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 0


def test_up_never_creates_a_platform_named_like_a_pipeline_stack(fake_cfn, s3, tmp_path, capsys):
    args = _up_args(stack="acme-polyris-dev", namespace="acme", stage="dev", admin_email="",
                    archive=str(_write_archive(tmp_path / "a.zip")))

    assert up._run(up.run_up, args) == 1

    assert "A platform stack may not be named 'acme-polyris-dev'" in _err(capsys)
    assert "create_change_set" not in fake_cfn["cfn"].names()


def test_up_refuses_a_task_that_runs_as_a_role_of_the_account_outside_the_tasks_path(
        fake_cfn, platform_1_3, s3, tmp_path, capsys):
    _pipeline(platform_1_3, "orders", role=f"arn:aws:iam::{ACCOUNT}:role/etl-role")
    _pipeline(platform_1_3, "reports", role=f"arn:aws:iam::{ACCOUNT}:role/polyris-tasks/finance-writer")
    _pipeline(platform_1_3, "warehouse", role="arn:aws:iam::222222222222:role/etl-role")
    _pipeline(platform_1_3, "basic")

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 1

    err = _err(capsys)
    assert "1 task(s) of deployed pipelines run as a role of this account outside /polyris-tasks/" in err
    assert f"pipeline orders, task load: arn:aws:iam::{ACCOUNT}:role/etl-role" in err
    assert "aws iam create-role --path /polyris-tasks/ --role-name etl-role" in err
    assert f"arn:aws:iam::{ACCOUNT}:role/polyris-tasks/etl-role" in err
    assert "reports" not in err and "warehouse" not in err
    assert "create_change_set" not in fake_cfn["cfn"].names()


def test_up_reads_only_each_pipelines_current_spec_version(fake_cfn, platform_1_3, s3, tmp_path):
    """A version an older deploy wrote stays in pipeline-specs for good; the registry names the current one."""
    _pipeline(platform_1_3, "orders", role=f"arn:aws:iam::{ACCOUNT}:role/etl-role")
    _pipeline(platform_1_3, "orders", role=f"arn:aws:iam::{ACCOUNT}:role/polyris-tasks/etl-role")

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 0


def test_up_skips_a_pipeline_deployed_before_1_3_which_has_no_specs(fake_cfn, platform_1_3, s3, tmp_path, capsys):
    _pipeline(platform_1_3, "old", specs=False, redeployed=False)
    fake_cfn["cfn"] = FakeCfn([_deployed()], updated={"Outputs": OUTPUTS_1_3})

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 0
    assert "polyris-deploy --select old" in capsys.readouterr().out


def test_up_accepts_the_scheduler_role_polyris_deploy_made(fake_cfn, platform_1_3, s3, tmp_path):
    _scheduler_role()
    _made_by_the_pipeline_stack(fake_cfn["cfn"])

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 0


def test_up_looks_for_a_scheduler_roles_stack_in_the_region_of_the_machine_it_starts(
        fake_cfn, platform_1_3, s3, tmp_path, mocker):
    """IAM is global, a stack regional: a pipeline of a platform in another region holds its scheduler role there.
    The stack is looked for in the region of the state machine the role starts; no tag says it."""
    machine = f"arn:aws:states:us-west-2:{ACCOUNT}:stateMachine:acme-dev-polyris-orders"
    name = SCHEDULER_ROLE_1_3["Properties"]["RoleName"]
    _scheduler_role(policy={"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "states:StartExecution", "Resource": machine}]})
    fake_cfn["cfn"].resources[name] = [
        {"StackName": "acme-dev-polyris-orders", "LogicalResourceId": "PipelineSchedulerRole", "PhysicalResourceId": name},
        {"StackName": "acme-dev-polyris-orders", "LogicalResourceId": "PipelineStateMachine",
         "PhysicalResourceId": machine}]
    regions: List[Any] = []
    routed = boto3.Session.client

    def client(self, service, *a, **kw):
        if service == "cloudformation":
            regions.append(kw.get("region_name"))
        return routed(self, service, *a, **kw)

    mocker.patch.object(boto3.Session, "client", client)

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 0
    assert "us-west-2" in regions


@pytest.mark.parametrize("plant, reason", [
    (lambda cfn: _scheduler_role(), "no pipeline stack made it"),
    (lambda cfn: (_scheduler_role(attach="Admin"),
                  _made_by_the_pipeline_stack(cfn)), "managed policies are attached to it"),
    (lambda cfn: (_scheduler_role(policy={"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "*", "Resource": "*"}]}), _made_by_the_pipeline_stack(cfn)),
     "its inline policies do more than start its pipeline's state machine"),
])
def test_up_refuses_a_scheduler_role_that_polyris_deploy_did_not_make(
        fake_cfn, platform_1_3, s3, tmp_path, capsys, plant, reason):
    plant(fake_cfn["cfn"])

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 1

    err = _err(capsys)
    assert "1 role(s) named like a pipeline's scheduler role are not what polyris-deploy made" in err
    assert f"arn:aws:iam::{ACCOUNT}:role/acme-dev-polyris-orders-scheduler-role: " in err and reason in err
    assert "CloudTrail's CreateRole, PutRolePolicy, AttachRolePolicy and UpdateAssumeRolePolicy" in err
    assert "Delete a role that is not a pipeline's." in err and "rename" not in err   # IAM renames no role
    assert "create_change_set" not in fake_cfn["cfn"].names()


def test_up_checks_the_scheduler_roles_of_a_new_install_too(fake_cfn, s3, tmp_path, capsys):
    """The new platform's deploy policy may pass such a role as soon as it exists."""
    _scheduler_role()
    args = _up_args(namespace="acme", stage="dev", admin_email="", archive=str(_write_archive(tmp_path / "a.zip")))

    assert up._run(up.run_up, args) == 1
    assert "not what polyris-deploy made" in _err(capsys)


def test_a_check_it_may_not_make_stops_polyris_up_with_the_permission(fake_cfn, platform_1_3, s3, tmp_path, capsys,
                                                                       mocker):
    real_client = boto3.Session.client

    def client(self, service, *a, **kw):
        made = real_client(self, service, *a, **kw)
        if service == "iam":
            mocker.patch.object(made, "get_paginator", side_effect=ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "no"}}, "ListRoles"))
        return made

    mocker.patch.object(boto3.Session, "client", client)

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 1
    assert "polyris-up may not call iam:ListRoles on *, which its checks before any change need." in _err(capsys)


def test_a_denied_read_of_the_deployed_pipelines_names_every_action_the_read_makes(
        fake_cfn, platform_1_3, s3, tmp_path, capsys, mocker):
    _pipeline(platform_1_3, "orders")
    real_client = boto3.Session.client

    def client(self, name, *a, **kw):
        made = real_client(self, name, *a, **kw)
        if name == "dynamodb":
            mocker.patch.object(made, "scan", side_effect=ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "Scan"))
        return made

    mocker.patch.object(boto3.Session, "client", client)

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 1
    assert "polyris-up may not call cloudformation:DescribeStacks, dynamodb:Scan, states:DescribeStateMachine" \
        in _err(capsys)


@pytest.mark.parametrize("operation", ["Query", "BatchGetItem"])
def test_a_denied_read_of_the_specs_names_every_action_the_read_makes(fake_cfn, platform_1_3, s3, tmp_path, capsys,
                                                                      mocker, operation):
    _pipeline(platform_1_3, "orders")
    real_client = boto3.Session.client
    method = {"Query": "query", "BatchGetItem": "batch_get_item"}[operation]

    def client(self, service, *a, **kw):
        made = real_client(self, service, *a, **kw)
        if service == "dynamodb":
            mocker.patch.object(made, method, side_effect=ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, operation))
        return made

    mocker.patch.object(boto3.Session, "client", client)

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 1
    assert f"polyris-up may not call dynamodb:Query, dynamodb:BatchGetItem on {SPECS}" in _err(capsys)


def test_up_deploys_only_the_template_it_uploaded(fake_cfn, platform_1_3, s3, tmp_path, capsys):
    """A writer of the bucket who swaps the template between the upload and the change set (ADR #142 §8)."""
    cfn = fake_cfn["cfn"]

    def swap(change_set):
        bucket, key = _s3_location(change_set["TemplateURL"])
        s3.put_object(Bucket=bucket, Key=key, Body=json.dumps({"Resources": {"Evil": {}}}).encode())
    cfn.on_create = swap

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 1

    assert "is not the one uploaded for acme-dev" in _err(capsys)
    assert "delete_change_set" in cfn.names() and "execute_change_set" not in cfn.names()


def test_up_uploads_its_template_under_a_key_nobody_can_predict(fake_cfn, platform_1_3, s3, tmp_path):
    archive = str(_write_archive(tmp_path / "a.zip"))
    urls = []
    for _ in range(2):
        fake_cfn["cfn"] = FakeCfn([_deployed()])
        assert up._run(up.run_up, _up_args(archive=archive)) == 0
        urls.append(fake_cfn["cfn"].last_change_set["TemplateURL"])
        assert "get_template" in fake_cfn["cfn"].names(), "the change set's template is checked"
    assert urls[0] != urls[1]


def test_the_update_to_1_4_tells_the_authors_to_take_the_new_deploy_policy(fake_cfn, platform_1_3, s3, tmp_path,
                                                                           capsys):
    fake_cfn["cfn"] = FakeCfn([_deployed()], **UPDATED_1_4)

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip", version="1.4.0")))) == 0

    out = capsys.readouterr().out
    assert (f"⚠ Pipeline authors need the new deploy policy: attach acme-dev-polyris-deploy-policy ({POLICY}) to "
            "their role or CI role and detach the old one.") in out
    assert "Order: platform (done) → attach the new policy → authors' CLI 1.4 → detach the old policy" in out
    assert f"Deploy policy for pipeline authors:  {POLICY}" in out


def test_the_outputs_name_the_role_a_tasks_own_role_trusts(fake_cfn, platform_1_3, s3, tmp_path, capsys):
    """The how-to's trust policy names the orchestration role: polyris-up prints its ARN with its outputs."""
    fake_cfn["cfn"] = FakeCfn([_deployed("1.4.0")], **UPDATED_1_4)

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip", version="1.4.1")))) == 0

    assert f"Orchestration role (a task's own role trusts it):  {ORCHESTRATION}" in capsys.readouterr().out


def test_an_update_within_1_4_says_nothing_of_the_policy_move(fake_cfn, platform_1_3, s3, tmp_path, capsys):
    fake_cfn["cfn"] = FakeCfn([_deployed("1.4.0")], **UPDATED_1_4)

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip", version="1.4.1")))) == 0
    assert "need the new deploy policy" not in capsys.readouterr().out


def test_up_lists_the_pipelines_that_still_need_a_redeploy_with_the_command_for_each(
        fake_cfn, platform_1_3, s3, tmp_path, capsys):
    """A pipeline deployed with an older format fails its runs until its redeploy: the list says which, and how."""
    _pipeline(platform_1_3, "orders", redeployed=False)
    _pipeline(platform_1_3, "customers")
    boto3.client("dynamodb", region_name=REGION).put_item(TableName=REGISTRY, Item={      # destroyed since
        "pipeline_name": {"S": "retired"}, "sfn_arn": {"S": f"{MACHINES}acme-dev-polyris-retired"}})
    fake_cfn["cfn"] = FakeCfn([_deployed(), {"StackName": "acme-dev-polyris-unregistered", "Tags": []}],
                              updated={"Outputs": OUTPUTS_1_3})
    args = _up_args(archive=str(_write_archive(tmp_path / "a.zip")), stack="acme-dev")

    assert up._run(up.run_up, args) == 0

    out = capsys.readouterr().out
    assert "2 pipeline(s) were deployed with an older pipeline format. Their runs fail at once" in out
    listed = [line.strip() for line in out.splitlines() if line.strip().startswith("polyris-deploy --select")]
    assert listed == [f"polyris-deploy --select {name} --stack acme-dev --region {REGION}"
                      for name in ("orders", "unregistered")]
    assert "polyris-deploy --all" in out
    # An update said "Next — a first pipeline" to a platform of 22 pipelines (1.3.0 on dev)
    assert "a first pipeline" not in out, "an update: the pipelines run as before"


def test_up_with_nothing_to_change_still_lists_the_pipelines_to_redeploy(fake_cfn, platform_1_3, s3, tmp_path, capsys):
    _pipeline(platform_1_3, "orders", redeployed=False)
    fake_cfn["cfn"] = FakeCfn([_deployed(__version__)], change_status="FAILED", status_reason="didn't contain changes")

    assert up._run(up.run_up, _up_args(archive=str(_write_archive(tmp_path / "a.zip")))) == 0

    out = capsys.readouterr().out
    assert "already up to date" in out and f"polyris-deploy --select orders --region {REGION}" in out


def test_down_refuses_while_a_pipeline_schedules_with_the_platforms_schedule_role(fake_cfn, s3, capsys):
    """CloudFormation would refuse to delete the export a pipeline stack imports (ADR #142 §4); --yes does not cover it."""
    fake_cfn["cfn"] = FakeCfn([_deployed("1.4.0", outputs=[])])
    fake_cfn["cfn"].imports["acme-dev-polyris-schedule-role-arn"] = ["acme-dev-polyris-orders"]

    assert up._run(up.run_down, _down_args(yes=True, delete_data=True)) == 1

    err = _err(capsys)
    assert "1 pipeline stack(s) still schedule with this platform's schedule role: acme-dev-polyris-orders." in err
    assert "Destroy the scheduled pipelines first" in err
    assert "delete_stack" not in fake_cfn["cfn"].names()
