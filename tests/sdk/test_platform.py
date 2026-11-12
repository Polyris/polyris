"""Tests for polyris/platform.py — the release contract behind polyris-up (ADR #131).

Pure logic, plus parity tests that pin the names it shares with
sam/template.yaml (Principle #28).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from polyris import platform as P

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = REPO_ROOT / "sam" / "template.yaml"


@pytest.fixture(scope="module")
def template() -> dict:
    class _Loader(yaml.SafeLoader):
        pass

    def _tag(loader, node):
        if isinstance(node, yaml.ScalarNode):
            return {"__tag__": node.tag, "value": node.value}
        if isinstance(node, yaml.SequenceNode):
            return {"__tag__": node.tag, "value": loader.construct_sequence(node)}
        return {"__tag__": node.tag, "value": loader.construct_mapping(node)}

    for tag in ("!Ref", "!Sub", "!GetAtt", "!Join", "!If", "!Not", "!Equals", "!And", "!Or",
                "!Condition", "!Select", "!Split", "!ImportValue", "!FindInMap"):
        _Loader.add_constructor(tag, _tag)
    return yaml.load(TEMPLATE_PATH.read_text(), Loader=_Loader)


# ── Parity with sam/template.yaml ────────────────────────────────────────────


@pytest.mark.parametrize("name", [
    P.PARAM_NAMESPACE, P.PARAM_STAGE, P.PARAM_ENABLE_COGNITO, P.PARAM_ADMIN_EMAIL,
    P.PARAM_ARTIFACTS_BUCKET, P.PARAM_UI_PREFIX,
])
def test_parameter_names_exist_in_template(template, name):
    assert name in template["Parameters"]


@pytest.mark.parametrize("name", [P.OUTPUT_CONSOLE_URL, P.OUTPUT_TEST_SFN_ARN])
def test_outputs_exist_in_template(template, name):
    assert name in template["Outputs"]


def test_bucket_names_match_template(template):
    """polyris-up pre-checks exactly the names the template will try to create."""
    sub = {
        lid: res["Properties"]["BucketName"]["value"]
        for lid, res in template["Resources"].items()
        if res["Type"] == "AWS::S3::Bucket"
    }
    results, console = P.platform_bucket_names("NS", "ST")
    assert sub["ResultsBucket"].replace("${Namespace}", "NS").replace("${Stage}", "ST") == results
    assert sub["ConsoleUiBucket"].replace("${Namespace}", "NS").replace("${Stage}", "ST") == console
    assert set(sub) == {"ResultsBucket", "ConsoleUiBucket"}, "a new bucket needs a pre-check too"


def test_namespace_stage_limit_matches_longest_role_name(template):
    """The 25-character budget is 64 minus the longest fixed part of any RoleName."""
    fixed = []
    for res in template["Resources"].values():
        role = (res.get("Properties") or {}).get("RoleName")
        if isinstance(role, dict) and role.get("__tag__") == "!Sub":
            text = role["value"]
            assert set(re.findall(r"\$\{(\w+)\}", text)) <= {"Namespace", "Stage"}
            fixed.append(len(text.replace("${Namespace}", "").replace("${Stage}", "")))
    assert P.NAMESPACE_STAGE_MAX_LEN == 64 - max(fixed)


def test_every_local_path_resource_is_rewritable(template):
    """Every CodeUri / DefinitionUri the template has is one rewrite_template handles."""
    for res in template["Resources"].values():
        props = res.get("Properties") or {}
        if "CodeUri" in props:
            assert res["Type"] == "AWS::Serverless::Function"
        if "DefinitionUri" in props:
            assert res["Type"] == "AWS::Serverless::StateMachine"


# ── Names and keys ───────────────────────────────────────────────────────────


def test_release_asset_url():
    assert P.release_asset_name("1.2.0") == "polyris-platform-v1.2.0.zip"
    assert P.release_asset_url("1.2.0") == (
        "https://github.com/Polyris/polyris/releases/download/v1.2.0/polyris-platform-v1.2.0.zip"
    )


def test_stack_and_bucket_names():
    assert P.default_stack_name("acme", "dev") == "acme-dev"
    assert P.platform_bucket_names("acme", "dev") == ("acme-dev-polyris-results", "acme-dev-polyris-console-ui")
    assert P.artifacts_bucket_name("123", "eu-central-1") == "polyris-artifacts-123-eu-central-1"


def test_artifact_keys_include_version_and_digest():
    digest = "a" * 64
    assert P.artifact_key("1.2.0", "lambdas", digest, "F.zip") == f"v1.2.0/lambdas/{'a' * 16}/F.zip"
    assert P.ui_prefix("1.2.0", digest) == f"v1.2.0/ui-{'a' * 16}"


@pytest.mark.parametrize("description,expected", [
    ("Polyris platform v1.2.0", "1.2.0"),
    ("Polyris platform v1.2.0 extra words", "1.2.0"),
    ("Polyris platform v", None),
    ("Polyris — Serverless Data Pipeline Orchestration Platform.", None),
    ("", None),
    (None, None),
])
def test_version_from_description(description, expected):
    assert P.version_from_description(description) == expected


# ── Namespace / Stage validation ─────────────────────────────────────────────


def test_valid_namespace_stage():
    assert P.validate_namespace_stage("acme", "dev") == []
    assert P.validate_namespace_stage("a-b1", "prod2") == []


@pytest.mark.parametrize("namespace,stage", [
    ("Acme", "dev"), ("acme_x", "dev"), ("-acme", "dev"), ("acme-", "dev"), ("", "dev"), ("acme", "d.v"),
])
def test_invalid_name_parts(namespace, stage):
    errors = P.validate_namespace_stage(namespace, stage)
    assert len(errors) == 1 and "lowercase" in errors[0]


def test_namespace_stage_too_long():
    errors = P.validate_namespace_stage("a" * 20, "b" * 6)
    assert errors == [
        "Namespace + Stage is 26 characters; the limit is 25 (IAM role names would exceed 64 characters)."
    ]


# ── Stack selection ──────────────────────────────────────────────────────────


def _stack(name: str, tagged: bool) -> dict:
    tags = [{"Key": "other", "Value": "x"}]
    if tagged:
        tags.append({"Key": P.PLATFORM_TAG_KEY, "Value": P.PLATFORM_TAG_VALUE})
    return {"StackName": name, "Tags": tags}


def test_select_platform_stack_none_one_many():
    assert P.select_platform_stack([_stack("a", False), {"StackName": "no-tags"}]) is None
    assert P.select_platform_stack([_stack("a", False), _stack("b", True)]) == "b"
    with pytest.raises(P.AmbiguousPlatformStack) as e:
        P.select_platform_stack([_stack("z", True), _stack("b", True)])
    assert e.value.names == ["b", "z"]


def test_tag_with_other_value_is_not_platform():
    assert not P.is_platform_stack({"Tags": [{"Key": P.PLATFORM_TAG_KEY, "Value": "artifacts"}]})


# ── Manifest ─────────────────────────────────────────────────────────────────


def _manifest_dict(**over) -> dict:
    d = P.Manifest(version="1.2.0", template="template.json", ui="ui",
                   functions={"B": "lambdas/B.zip", "A": "lambdas/A.zip"},
                   state_machines={"S": "sfn/S.json"}).to_dict()
    d.update(over)
    return d


def test_manifest_round_trip_sorts_maps():
    d = _manifest_dict()
    assert list(d["functions"]) == ["A", "B"]
    m = P.Manifest.from_dict(d)
    assert m.functions == {"A": "lambdas/A.zip", "B": "lambdas/B.zip"}
    assert m.to_dict() == d


@pytest.mark.parametrize("bad,message", [
    ([], "must be an object"),
    (_manifest_dict(format=2), "format 2 is not supported"),
    (_manifest_dict(version=""), "'version' must be a non-empty string"),
    (_manifest_dict(template=None), "'template' must be a non-empty string"),
    (_manifest_dict(functions=["x"]), "'functions' must map"),
    (_manifest_dict(state_machines={"S": 1}), "'state_machines' must map"),
])
def test_manifest_rejects_bad_input(bad, message):
    with pytest.raises(ValueError, match=message):
        P.Manifest.from_dict(bad)


# ── Template rewrite ─────────────────────────────────────────────────────────


def _tpl() -> dict:
    return {
        "Description": "old",
        "Resources": {
            "Fn": {"Type": "AWS::Serverless::Function", "Properties": {"CodeUri": "lambdas/fn/"}},
            "Sm": {"Type": "AWS::Serverless::StateMachine", "Properties": {"DefinitionUri": "sfn/x.json"}},
            "Inline": {"Type": "AWS::StepFunctions::StateMachine", "Properties": {"DefinitionString": "{}"}},
        },
    }


def test_rewrite_template_points_at_s3_and_stamps_version():
    original = _tpl()
    out = P.rewrite_template(original, bucket="bkt", function_keys={"Fn": "k1"},
                             state_machine_keys={"Sm": "k2"}, version="1.2.0")
    assert out["Resources"]["Fn"]["Properties"]["CodeUri"] == {"Bucket": "bkt", "Key": "k1"}
    assert out["Resources"]["Sm"]["Properties"]["DefinitionUri"] == {"Bucket": "bkt", "Key": "k2"}
    assert out["Description"] == "Polyris platform v1.2.0"
    assert original["Resources"]["Fn"]["Properties"]["CodeUri"] == "lambdas/fn/", "input not mutated"


def test_rewrite_template_rejects_unknown_logical_id():
    with pytest.raises(ValueError, match="no AWS::Serverless::Function named 'Nope'"):
        P.rewrite_template(_tpl(), bucket="b", function_keys={"Fn": "k", "Nope": "k"},
                           state_machine_keys={"Sm": "k"}, version="1")


def test_rewrite_template_rejects_wrong_type():
    with pytest.raises(ValueError, match="no AWS::Serverless::StateMachine named 'Fn'"):
        P.rewrite_template(_tpl(), bucket="b", function_keys={"Fn": "k"},
                           state_machine_keys={"Fn": "k"}, version="1")


def test_rewrite_template_rejects_unmapped_local_path():
    with pytest.raises(ValueError, match="No artifact for DefinitionUri of: Sm"):
        P.rewrite_template(_tpl(), bucket="b", function_keys={"Fn": "k"}, state_machine_keys={}, version="1")


def test_rewrite_template_without_resources():
    assert P.rewrite_template({}, bucket="b", function_keys={}, state_machine_keys={},
                              version="1")["Description"] == "Polyris platform v1"


def test_rewrite_template_adds_missing_properties_block():
    tpl = {"Resources": {"Fn": {"Type": "AWS::Serverless::Function"}}}
    out = P.rewrite_template(tpl, bucket="b", function_keys={"Fn": "k"}, state_machine_keys={}, version="1")
    assert out["Resources"]["Fn"]["Properties"]["CodeUri"] == {"Bucket": "b", "Key": "k"}


# ── Parameters ───────────────────────────────────────────────────────────────


def test_parse_param_overrides():
    assert P.parse_param_overrides(["EnablePitr=true", " X =a=b", "Empty="]) == {
        "EnablePitr": "true", "X": "a=b", "Empty": "",
    }
    for bad in ("NoEquals", "=value"):
        with pytest.raises(ValueError, match="Key=Value"):
            P.parse_param_overrides([bad])


def test_resolve_parameters_create_passes_only_values():
    params = P.resolve_parameters(template_params=["Namespace", "Stage", "EnablePitr"],
                                  existing=None, values={"Stage": "dev", "Namespace": "acme"})
    assert params == [
        {"ParameterKey": "Namespace", "ParameterValue": "acme"},
        {"ParameterKey": "Stage", "ParameterValue": "dev"},
    ]


def test_resolve_parameters_update_keeps_previous_values():
    params = P.resolve_parameters(
        template_params=["Namespace", "Stage", "EnablePitr", "NewInThisVersion"],
        existing={"Namespace": "acme", "Stage": "dev", "EnablePitr": "false", "RemovedParam": "x"},
        values={"EnablePitr": "true"},
    )
    assert params == [
        {"ParameterKey": "EnablePitr", "ParameterValue": "true"},
        {"ParameterKey": "Namespace", "UsePreviousValue": True},
        {"ParameterKey": "Stage", "UsePreviousValue": True},
    ]


def test_resolve_parameters_rejects_unknown_keys():
    with pytest.raises(ValueError, match="Unknown template parameter\\(s\\): Typo"):
        P.resolve_parameters(template_params=["A"], existing=None, values={"Typo": "1"})


def test_is_platform_shaped():
    tagged = {"Tags": [{"Key": P.PLATFORM_TAG_KEY, "Value": P.PLATFORM_TAG_VALUE}]}
    from_source = {"Parameters": [{"ParameterKey": k, "ParameterValue": "x"}
                                  for k in ("Namespace", "Stage", "EnableCognitoAuth", "LogRetentionDays")]}
    unrelated = {"Parameters": [{"ParameterKey": "Namespace", "ParameterValue": "x"}]}
    assert P.is_platform_shaped(tagged) and P.is_platform_shaped(from_source)
    assert not P.is_platform_shaped(unrelated) and not P.is_platform_shaped({})


@pytest.mark.parametrize("key", sorted(P.RESERVED_PARAMS))
def test_reserved_params_are_refused(key):
    with pytest.raises(ValueError, match=f"can't set {key}"):
        P.parse_param_overrides([f"{key}=x"])


def test_change_set_guards():
    assert P.platform_tags({"Tags": [{"Key": "a", "Value": "b"}]})[-1]["Key"] == P.PLATFORM_TAG_KEY
    assert P.replaced_data_resources({"Changes": [
        {"ResourceChange": {"LogicalResourceId": "U", "ResourceType": "AWS::Cognito::UserPoolUser",
                            "Action": "Remove"}}]}) == ["U (AWS::Cognito::UserPoolUser, Remove)"]
    assert P.parameters_of({"Parameters": [{"ParameterKey": "A", "ParameterValue": "1"}]}) == {"A": "1"}


def test_cognito_has_no_self_sign_up(template):
    """Every Cognito user is a console admin (console_api auth.py), so sign-up must be admin-only."""
    pool = template["Resources"]["CognitoUserPool"]["Properties"]
    assert pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True
