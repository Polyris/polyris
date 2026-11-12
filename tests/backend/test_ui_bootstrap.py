"""Tests for the UiBootstrap custom-resource Lambda (ADR #131).

AWS is mocked at the SDK boundary with moto (S3, CloudFront); the
CloudFormation response is captured at ``urllib.request.urlopen``, which is
where ``cfnresponse`` leaves the process. The parent-stack status lookup is
patched on the boto3 client because moto cannot put a stack into
DELETE_IN_PROGRESS on demand.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_aws

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "sam" / "lambdas" / "ui_bootstrap"
TEMPLATE_PATH = REPO_ROOT / "sam" / "template.yaml"

DEST = "acme-dev-polyris-console-ui"
SRC = "polyris-artifacts-111111111111-us-east-1"
PREFIX = "v1.2.0/ui-abc123"


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, LAMBDA_DIR / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        yield


@pytest.fixture
def lam(aws):
    """Load the Lambda inside the moto context so its module-level clients are mocked."""
    # index.py imports these two by bare name, as it does in the Lambda runtime.
    _load_module("cfnresponse", "cfnresponse.py")
    _load_module("config_js", "config_js.py")
    return _load_module("ui_bootstrap_index", "index.py")


@pytest.fixture
def responses(mocker):
    """Capture every CloudFormation response the Lambda PUTs."""
    sent: list[dict] = []

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(req, timeout=None):
        sent.append(json.loads(req.data.decode()))
        return _Resp()

    mocker.patch("urllib.request.urlopen", side_effect=_urlopen)
    return sent


@pytest.fixture
def distribution_id(aws):
    cf = boto3.client("cloudfront", region_name="us-east-1")
    dist = cf.create_distribution(DistributionConfig={
        "CallerReference": "test",
        "Comment": "",
        "Enabled": True,
        "Origins": {"Quantity": 1, "Items": [{
            "Id": "s3", "DomainName": f"{DEST}.s3.amazonaws.com",
            "S3OriginConfig": {"OriginAccessIdentity": ""},
        }]},
        "DefaultCacheBehavior": {
            "TargetOriginId": "s3", "ViewerProtocolPolicy": "redirect-to-https",
            "TrustedSigners": {"Enabled": False, "Quantity": 0},
            "ForwardedValues": {"QueryString": False, "Cookies": {"Forward": "none"}},
            "MinTTL": 0,
        },
    })
    return dist["Distribution"]["Id"]


def _buckets(source_files: dict[str, str], dest_files: dict[str, str] | None = None):
    s3 = boto3.client("s3", region_name="us-east-1")
    for bucket in (SRC, DEST):
        s3.create_bucket(Bucket=bucket)
    for key, body in source_files.items():
        s3.put_object(Bucket=SRC, Key=f"{PREFIX}/{key}", Body=body.encode())
    for key, body in (dest_files or {}).items():
        s3.put_object(Bucket=DEST, Key=key, Body=body.encode())
    return s3


def _event(request_type: str, distribution_id: str, **props_override):
    props = {
        "DestBucket": DEST,
        "SourceBucket": SRC,
        "SourcePrefix": PREFIX,
        "ApiUrl": "https://abc.execute-api.us-east-1.amazonaws.com/dev/api",
        "CognitoUserPoolId": "us-east-1_pool",
        "CognitoClientId": "client123",
        "Region": "us-east-1",
        "DistributionId": distribution_id,
    }
    props.update(props_override)
    return {
        "RequestType": request_type,
        "StackId": "arn:aws:cloudformation:us-east-1:111111111111:stack/acme-dev/x",
        "RequestId": f"req-{request_type}",
        "LogicalResourceId": "UiBootstrap",
        "ResponseURL": "https://cfn-response.example/put",
        "ResourceProperties": props,
    }


class _Ctx:
    log_stream_name = "log-stream-that-changes-every-invocation"


def _dest_keys(s3) -> set[str]:
    return {o["Key"] for o in s3.list_objects_v2(Bucket=DEST).get("Contents", [])}


# --------------------------------------------------------------------------
# Create / Update
# --------------------------------------------------------------------------


def test_create_installs_ui_prunes_stale_and_writes_config(lam, responses, distribution_id):
    s3 = _buckets(
        {"index.html": "<html>", "_next/static/app-1.js": "js", "config.js": "STALE CONFIG"},
        dest_files={"_next/static/app-0.js": "old build", "config.js": "old"},
    )

    lam.handler(_event("Create", distribution_id), _Ctx())

    assert responses[-1]["Status"] == "SUCCESS"
    assert responses[-1]["Data"] == {"FilesCopied": "2"}
    # Old build file pruned; config.js kept (re-rendered), never copied from source.
    assert _dest_keys(s3) == {"index.html", "_next/static/app-1.js", "config.js"}
    config = s3.get_object(Bucket=DEST, Key="config.js")["Body"].read().decode()
    assert "STALE CONFIG" not in config
    assert '"https://abc.execute-api.us-east-1.amazonaws.com/dev/api"' in config
    assert "enabled: true" in config


def test_copied_objects_get_cache_control_and_content_type(lam, responses, distribution_id):
    s3 = _buckets({"index.html": "<html>", "_next/static/app.js": "js"})

    lam.handler(_event("Create", distribution_id), _Ctx())

    html = s3.head_object(Bucket=DEST, Key="index.html")
    js = s3.head_object(Bucket=DEST, Key="_next/static/app.js")
    cfg = s3.head_object(Bucket=DEST, Key="config.js")
    assert (html["CacheControl"], html["ContentType"]) == (lam.NO_CACHE, "text/html")
    assert (js["CacheControl"], js["ContentType"]) == (lam.IMMUTABLE, "application/javascript")
    assert (cfg["CacheControl"], cfg["ContentType"]) == (lam.NO_CACHE, "application/javascript")


def test_create_invalidates_cloudfront(lam, responses, distribution_id):
    _buckets({"index.html": "<html>"})

    lam.handler(_event("Create", distribution_id), _Ctx())

    cf = boto3.client("cloudfront", region_name="us-east-1")
    items = cf.list_invalidations(DistributionId=distribution_id)["InvalidationList"]["Items"]
    assert len(items) == 1


def test_empty_source_fails_and_leaves_live_ui_untouched(lam, responses, distribution_id):
    s3 = _buckets({}, dest_files={"index.html": "live", "config.js": "live"})

    lam.handler(_event("Update", distribution_id), _Ctx())

    assert responses[-1]["Status"] == "FAILED"
    assert "No UI files found" in responses[-1]["Reason"]
    assert _dest_keys(s3) == {"index.html", "config.js"}


def test_physical_resource_id_is_stable_across_create_and_update(lam, responses, distribution_id):
    """A changing id makes CloudFormation Delete the 'old' resource after every Update."""
    _buckets({"index.html": "<html>"})

    lam.handler(_event("Create", distribution_id), _Ctx())
    lam.handler(_event("Update", distribution_id), _Ctx())

    ids = {r["PhysicalResourceId"] for r in responses}
    assert ids == {f"ui-bootstrap:{DEST}"}


def test_config_without_cognito_disables_auth(lam, responses, distribution_id):
    s3 = _buckets({"index.html": "<html>"})

    lam.handler(_event("Create", distribution_id, CognitoUserPoolId="", CognitoClientId=""), _Ctx())

    config = s3.get_object(Bucket=DEST, Key="config.js")["Body"].read().decode()
    assert "enabled: false" in config


# --------------------------------------------------------------------------
# Delete
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status,emptied", [
    ("DELETE_IN_PROGRESS", True),
    ("ROLLBACK_IN_PROGRESS", True),
    ("UPDATE_COMPLETE_CLEANUP_IN_PROGRESS", False),  # resource removed by an update
])
def test_delete_empties_bucket_only_on_teardown(lam, responses, distribution_id, mocker, status, emptied):
    s3 = _buckets({}, dest_files={"index.html": "live", "config.js": "live"})
    mocker.patch.object(lam.cfn, "describe_stacks", return_value={"Stacks": [{"StackStatus": status}]})

    lam.handler(_event("Delete", distribution_id), _Ctx())

    assert responses[-1]["Status"] == "SUCCESS"
    assert (_dest_keys(s3) == set()) is emptied


def test_delete_keeps_files_when_stack_status_unknown(lam, responses, distribution_id, mocker):
    s3 = _buckets({}, dest_files={"index.html": "live"})
    mocker.patch.object(lam.cfn, "describe_stacks", side_effect=RuntimeError("throttled"))

    lam.handler(_event("Delete", distribution_id), _Ctx())

    assert responses[-1]["Status"] == "SUCCESS"
    assert _dest_keys(s3) == {"index.html"}


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key,expected", [
    ("index.html", "text/html"),
    ("_next/static/chunks/a.js", "application/javascript"),
    ("_next/static/css/a.CSS", "text/css"),
    ("index.txt", "text/plain"),
    ("fonts/a.woff2", "font/woff2"),
    ("logo.png", "image/png"),
    ("LICENSE", "application/octet-stream"),
])
def test_content_type_for(lam, key, expected):
    assert lam.content_type_for(key) == expected


# --------------------------------------------------------------------------
# Contracts with the template and the shell deploy scripts (Principle #28)
# --------------------------------------------------------------------------


def _template() -> dict:
    class _Loader(yaml.SafeLoader):
        pass

    def _tag(loader, node):
        if isinstance(node, yaml.ScalarNode):
            return {"__tag__": node.tag, "value": node.value}
        if isinstance(node, yaml.SequenceNode):
            return {"__tag__": node.tag, "value": loader.construct_sequence(node)}
        return {"__tag__": node.tag, "value": loader.construct_mapping(node)}

    for tag in ("!Ref", "!Sub", "!GetAtt", "!If", "!Not", "!Equals", "!And", "!Condition"):
        _Loader.add_constructor(tag, _tag)
    return yaml.load(TEMPLATE_PATH.read_text(), Loader=_Loader)


def test_template_api_url_matches_deploy_sh():
    """ui/deploy.sh writes ConsoleApiUrl + '/api'; UiBootstrap must pass the same URL."""
    tpl = _template()
    api_url = tpl["Resources"]["UiBootstrap"]["Properties"]["ApiUrl"]["value"]
    console_api_url = tpl["Outputs"]["ConsoleApiUrl"]["Value"]["value"]
    assert api_url == console_api_url + "/api"
    assert 'API_URL="${API_URL_RAW}/api"' in (REPO_ROOT / "ui" / "deploy.sh").read_text()


def test_template_passes_every_property_the_lambda_reads():
    props = set(_template()["Resources"]["UiBootstrap"]["Properties"])
    read = set(re.findall(r'props(?:\.get)?\(?\[?"(\w+)"', (LAMBDA_DIR / "index.py").read_text()))
    assert read <= props, f"index.py reads properties the template never passes: {read - props}"


def _config_keys(js: str) -> list[str]:
    body = js[js.index("window.CONFIG = {"):]
    return re.findall(r"^\s*(\w+):", body, flags=re.MULTILINE)


@pytest.mark.parametrize("script", ["ui/deploy.sh", "sam/deploy-ui.sh"])
def test_shell_config_js_matches_renderer(script):
    """The shell scripts' config.js heredoc has the same fields, in order, as config_js.py."""
    renderer = _load_module("config_js", "config_js.py")
    rendered = renderer.render_config_js(api_url="a", user_pool_id="p", client_id="c", region="r")
    shell = (REPO_ROOT / script).read_text()
    assert _config_keys(shell) == _config_keys(rendered)
    assert f"POLL_INTERVAL: {renderer.POLL_INTERVAL_MS}" in shell


def test_vendored_cfnresponse_matches_bucket_cleanup_copy():
    """Both custom-resource Lambdas ship the same stdlib cfnresponse (no drift between copies)."""
    ours = (LAMBDA_DIR / "cfnresponse.py").read_bytes()
    theirs = (REPO_ROOT / "sam" / "lambdas" / "bucket_cleanup" / "cfnresponse.py").read_bytes()
    assert ours == theirs
