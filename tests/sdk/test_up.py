"""Tests for polyris-up / polyris-down (polyris/up.py, ADR #131).

Boundaries (Principle #14): S3 and STS run on moto; the release download is
mocked at ``urllib.request.urlopen``. CloudFormation is a small fake client,
because moto cannot expand the SAM transform or run change sets — the fake
records every call so the tests can assert the exact request polyris-up
sends.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any, Dict, List

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

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
    """Records calls; answers like CloudFormation for the paths polyris-up uses."""

    def __init__(self, stacks: List[Dict[str, Any]] | None = None, change_status: str = "CREATE_COMPLETE",
                 status_reason: str = "", changes: List[Dict[str, Any]] | None = None):
        self.stacks = {s["StackName"]: s for s in (stacks or [])}
        self.change_status = change_status
        self.status_reason = status_reason
        self.changes = changes or [{"ResourceChange": {"Action": "Add", "ResourceType": "AWS::Lambda::Function"}}]
        self.calls: List[tuple] = []

    # describe_stacks / paginator
    def describe_stacks(self, StackName=None):
        self.calls.append(("describe_stacks", StackName))
        if StackName is None:
            return {"Stacks": list(self.stacks.values())}
        if StackName not in self.stacks:
            raise ClientError({"Error": {"Code": "ValidationError",
                                         "Message": f"Stack with id {StackName} does not exist"}}, "DescribeStacks")
        return {"Stacks": [self.stacks[StackName]]}

    def get_paginator(self, name):
        fake = self

        class _P:
            def paginate(self, **kw):
                if name == "describe_stacks":
                    return [fake.describe_stacks()]
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
        self.stacks[kw["StackName"]] = {
            "StackName": kw["StackName"], "Tags": kwargs["Tags"], "StackStatus": "CREATE_COMPLETE",
            "Outputs": [{"OutputKey": "ConsoleUiUrl", "OutputValue": "https://d123.cloudfront.net"}],
        }

    def delete_stack(self, StackName):
        self.calls.append(("delete_stack", StackName))
        self.stacks.pop(StackName)

    def get_waiter(self, name):
        class _W:
            def wait(self, **kw):
                return None
        return _W()

    def names(self) -> List[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def fake_cfn(aws, mocker):
    """Route the session's CloudFormation client to a FakeCfn; S3/STS stay on moto."""
    holder: Dict[str, FakeCfn] = {"cfn": FakeCfn()}
    real_client = boto3.Session.client

    def client(self, service, *a, **kw):
        if service == "cloudformation":
            return holder["cfn"]
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
    up.ensure_artifacts_bucket(s3, "polyris-artifacts-1-eu", REGION)
    up.ensure_artifacts_bucket(s3, "polyris-artifacts-1-eu", REGION)  # idempotent
    block = s3.get_public_access_block(Bucket="polyris-artifacts-1-eu")["PublicAccessBlockConfiguration"]
    assert all(block.values())
    enc = s3.get_bucket_encryption(Bucket="polyris-artifacts-1-eu")
    assert enc["ServerSideEncryptionConfiguration"]["Rules"][0]["ApplyServerSideEncryptionByDefault"]
    assert s3.get_bucket_location(Bucket="polyris-artifacts-1-eu")["LocationConstraint"] == REGION


def test_bucket_owner_status(s3, mocker):
    assert up.bucket_owner_status(s3, "nobody-has-this") == "missing"
    s3.create_bucket(Bucket="mine", CreateBucketConfiguration={"LocationConstraint": REGION})
    assert up.bucket_owner_status(s3, "mine") == "ours"
    mocker.patch.object(s3, "head_bucket", side_effect=ClientError({"Error": {"Code": "403"}}, "HeadBucket"))
    assert up.bucket_owner_status(s3, "theirs") == "taken"
    with pytest.raises(up.UpError, match="belongs to another account"):
        up.ensure_artifacts_bucket(s3, "theirs", REGION)


def test_upload_artifacts_is_content_addressed_and_idempotent(s3, tmp_path, capsys):
    root, manifest = up.extract_archive(_write_archive(tmp_path / "a.zip"))
    bucket = "polyris-artifacts"
    s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION})

    fkeys, skeys, prefix = up.upload_artifacts(s3, bucket, root, manifest)
    again = up.upload_artifacts(s3, bucket, root, manifest)

    assert again == (fkeys, skeys, prefix)
    assert "2 uploaded" not in capsys.readouterr().out.splitlines()[-1]  # second run uploads nothing
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
    assert up.count_objects(s3, "versioned-bkt") == 3
    assert up.empty_bucket(s3, "versioned-bkt") == 3
    assert up.count_objects(s3, "versioned-bkt") == 0
    assert up.count_objects(s3, "gone") == 0
    assert up.empty_bucket(s3, "gone") == 0


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


def test_up_update_keeps_previous_parameters(fake_cfn, s3, tmp_path):
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="UPDATE_COMPLETE", Description="Polyris platform v1.0.0",
                                         Parameters=[{"ParameterKey": "Namespace", "ParameterValue": "acme"},
                                                     {"ParameterKey": "AdminEmail", "ParameterValue": "a@b.c"}])])
    args = _up_args(archive=str(_write_archive(tmp_path / "a.zip")), param=["EnablePitr=true"])

    assert up._run(up.run_up, args) == 0

    cs = fake_cfn["cfn"].last_change_set
    assert cs["ChangeSetType"] == "UPDATE"
    params = _params(cs)
    assert params["Namespace"] == "<previous>" and params["AdminEmail"] == "<previous>"
    assert params["EnablePitr"] == "true"


def test_up_no_changes(fake_cfn, s3, tmp_path, capsys):
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="UPDATE_COMPLETE", Parameters=[])],
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
    args = _up_args(namespace="Acme_Corp", stage="dev", archive=str(tmp_path / "unused.zip"))
    assert up._run(up.run_up, args) == 1
    assert "lowercase" in capsys.readouterr().err


def test_up_without_tty_needs_namespace(fake_cfn, capsys):
    assert up._run(up.run_up, _up_args()) == 1
    assert "stdin is not a terminal" in capsys.readouterr().err


def test_up_refuses_busy_stack(fake_cfn, capsys):
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="UPDATE_IN_PROGRESS")])
    assert up._run(up.run_up, _up_args()) == 1
    assert "busy" in capsys.readouterr().err


def test_up_data_replacement_needs_confirmation(fake_cfn, s3, tmp_path, capsys):
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="UPDATE_COMPLETE", Parameters=[])], changes=[
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
    assert up.count_objects(s3, "acme-dev-polyris-console-ui") == 1, "nothing emptied"


def test_down_with_delete_data_empties_and_deletes(fake_cfn, s3, capsys):
    fake_cfn["cfn"] = FakeCfn([_stack_with_buckets(s3, results_objects=2)])
    up.ensure_artifacts_bucket(s3, artifacts_bucket_name(ACCOUNT, REGION), REGION)

    assert up._run(up.run_down, _down_args(yes=True, delete_data=True)) == 0

    assert "delete_stack" in fake_cfn["cfn"].names()
    assert up.count_objects(s3, "acme-dev-polyris-results") == 0
    assert up.bucket_owner_status(s3, artifacts_bucket_name(ACCOUNT, REGION)) == "missing"
    assert "1 DynamoDB tables" in capsys.readouterr().out


def test_down_keeps_artifacts_bucket_while_other_platforms_exist(fake_cfn, s3, capsys):
    fake_cfn["cfn"] = FakeCfn([_stack_with_buckets(s3, results_objects=0), _platform("acme-prod")])
    up.ensure_artifacts_bucket(s3, artifacts_bucket_name(ACCOUNT, REGION), REGION)

    assert up._run(up.run_down, _down_args(yes=True, stack="acme-dev")) == 0

    assert up.bucket_owner_status(s3, artifacts_bucket_name(ACCOUNT, REGION)) == "ours"
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
    assert up._run(up.run_up, _up_args()) == 1
    assert "polyris-down --stack acme-dev" in capsys.readouterr().err


def _sam_stack(name="legacy-dev", namespace="acme", stage="dev"):
    """A stack deployed with `sam deploy`: no platform tag, platform parameters."""
    return {"StackName": name, "StackStatus": "UPDATE_COMPLETE", "Tags": [{"Key": "team", "Value": "data"}],
            "Parameters": [{"ParameterKey": "Namespace", "ParameterValue": namespace},
                           {"ParameterKey": "Stage", "ParameterValue": stage},
                           {"ParameterKey": "EnableCognitoAuth", "ParameterValue": "true"}]}


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
    assert "acme-dev-polyris-orders" in err and "polyris-destroy" in err
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
    assert up.count_objects(s3, "unrelated-datalake") == 1
    assert "delete_stack" not in fake_cfn["cfn"].names()


def test_down_refuses_a_busy_stack(fake_cfn, capsys):
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="UPDATE_IN_PROGRESS")])
    assert up._run(up.run_down, _down_args(yes=True)) == 1
    assert "busy" in capsys.readouterr().err


def test_yes_does_not_cover_data_loss(fake_cfn, s3, tmp_path, capsys):
    replace_table = [{"ResourceChange": {"LogicalResourceId": "PipelineTokensTable",
                                         "ResourceType": "AWS::DynamoDB::Table",
                                         "Action": "Modify", "Replacement": "True"}}]
    fake_cfn["cfn"] = FakeCfn([_platform("acme-dev", StackStatus="UPDATE_COMPLETE", Parameters=[])],
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
