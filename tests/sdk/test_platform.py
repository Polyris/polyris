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


@pytest.mark.parametrize("name", [P.OUTPUT_CONSOLE_URL, P.OUTPUT_TEST_SFN_ARN, P.OUTPUT_TOKENS_TABLE,
                                  P.OUTPUT_REGISTRY_TABLE, P.OUTPUT_SPECS_TABLE, P.OUTPUT_START_RUN])
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
    """The budget of Namespace + Stage is 64 minus the longest fixed part of any RoleName."""
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
    limit = P.NAMESPACE_STAGE_MAX_LEN
    assert P.validate_namespace_stage("a" * (limit - 6), "b" * 6) == []
    errors = P.validate_namespace_stage("a" * (limit - 5), "b" * 6)
    assert errors == [
        f"Namespace + Stage is {limit + 1} characters; the limit is {limit} (IAM role names would exceed 64 characters)."
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
    with pytest.raises(ValueError, match="No artifact for local path\\(s\\): Sm.DefinitionUri"):
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



# ── Code-review findings (PR #51) ────────────────────────────────────────────


def test_namespace_must_start_with_a_letter():
    assert P.validate_namespace_stage("3m", "dev") == [
        "Namespace '3m' must start with a letter (it starts the stack name)."]
    assert P.validate_namespace_stage("acme", "1") == []  # stage may start with a digit


def test_discovery_skips_dead_and_optionally_pending_stacks():
    def st(name, status):
        return {"StackName": name, "StackStatus": status,
                "Tags": [{"Key": P.PLATFORM_TAG_KEY, "Value": P.PLATFORM_TAG_VALUE}]}
    stacks = [st("acme-dev", "UPDATE_COMPLETE"), st("acme-prod", "ROLLBACK_COMPLETE"), st("acme-qa", "REVIEW_IN_PROGRESS")]
    with pytest.raises(P.AmbiguousPlatformStack):  # dev + pending qa
        P.select_platform_stack(stacks)
    assert P.select_platform_stack(stacks, include_review=False) == "acme-dev"
    with pytest.raises(P.AmbiguousPlatformStack) as e:
        P.select_platform_stack(stacks, include_dead=True, include_review=False)
    assert e.value.names == ["acme-dev", "acme-prod"]


def test_rewrite_template_rejects_any_unpackaged_local_path():
    """A new kind of local path (e.g. a layer) fails loudly instead of at deploy time."""
    tpl = _tpl()
    tpl["Resources"]["Layer"] = {"Type": "AWS::Serverless::LayerVersion", "Properties": {"ContentUri": "layers/x/"}}
    tpl["Resources"]["Remote"] = {"Type": "AWS::Serverless::LayerVersion",
                                  "Properties": {"ContentUri": "s3://bucket/key.zip"}}
    with pytest.raises(ValueError, match="Layer.ContentUri"):
        P.rewrite_template(tpl, bucket="b", function_keys={"Fn": "k"}, state_machine_keys={"Sm": "k"}, version="1")


@pytest.mark.parametrize("deployed,installing,expected", [
    ("1.2.0", "1.1.3", True),
    ("1.2.0", "1.2.0", False),
    ("1.2.0", "1.10.0", False),
    (None, "1.0.0", False),
    ("1.2.0", "0.0.0-dev", False),  # unknown versions never block
    # A pre-release (PEP 440) is older than its release, and newer than the release before it.
    ("1.3.0", "1.3.0rc1", True),
    ("1.3.0rc1", "1.3.0", False),
    ("1.3.0rc1", "1.2.3", True),
    ("1.2.3", "1.3.0rc1", False),
    ("1.3.0rc2", "1.3.0rc1", True),
    ("1.3.0rc1", "1.3.0b2", True),
    ("1.3.0b1", "1.3.0a2", True),
    ("1.3.0rc1", "1.3.0rc1", False),
])
def test_is_downgrade(deployed, installing, expected):
    assert P.is_downgrade(deployed, installing) is expected


def test_parse_version_orders_a_pre_release_before_its_release():
    assert P.parse_version("1.10.2") == ((1, 10, 2), (3, 0))
    assert P.parse_version("1.3.0rc1") == ((1, 3, 0), (2, 1))
    in_order = ["1.2.3", "1.3.0a1", "1.3.0a2", "1.3.0b1", "1.3.0rc1", "1.3.0rc2", "1.3.0", "1.3.1", "1.10.0"]
    assert sorted(reversed(in_order), key=P.parse_version) == in_order


@pytest.mark.parametrize("unknown", [None, "", "v1.0", "0.0.0-dev", "1.3.0rc", "1.3.0-rc1", "1.3.0rc1x", "1.3.0.dev1"])
def test_parse_version_reads_nothing_else(unknown):
    assert P.parse_version(unknown) is None


def test_this_polyris_has_a_version_polyris_up_can_order():
    """A version parse_version cannot read never counts as a rollback, and names no release for the
    checks of an upgrade: a branch that carries a pre-release must carry one polyris-up orders."""
    import polyris

    assert P.parse_version(polyris.__version__) is not None



def test_cognito_has_no_self_sign_up(template):
    """Every Cognito user is a console admin (console_api auth.py), so sign-up must be admin-only."""
    pool = template["Resources"]["CognitoUserPool"]["Properties"]
    assert pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True


# ── The platform's state machines, and the upgrade's rules (PLAN_IAM 1.4.0 §4) ─────────────


def _state_machines(template) -> dict:
    """Every state machine of the template: {its name after the platform's prefix: STANDARD | EXPRESS}."""
    prefix = P.platform_prefix("${Namespace}", "${Stage}")
    found = {}
    for res in template["Resources"].values():
        if res["Type"].endswith("::StateMachine"):
            props = res["Properties"]
            name = (props.get("StateMachineName") or props["Name"])["value"]
            assert name.startswith(prefix), name
            found[name[len(prefix):]] = props.get("StateMachineType") or props.get("Type") or "STANDARD"
    return found


def test_the_engines_state_machines_are_named_as_the_template_names_them(template):
    """The deny lists and the reserved dag_ids name them so (Principle #28)."""
    standard = {name for name, kind in _state_machines(template).items() if kind == "STANDARD"}
    assert standard == set(P.PLATFORM_NAMES.state_machines)


def test_platform_names_and_outputs():
    assert P.platform_prefix("acme", "dev") == "acme-dev-polyris-"
    assert P.outputs_of({"Outputs": [{"OutputKey": "A", "OutputValue": "1"}, {"OutputKey": "B"}]}) == {"A": "1", "B": ""}
    assert P.outputs_of({}) == {}


def test_release_series():
    assert P.release_series("1.3.0rc1") == (1, 3) == P.release_series("1.3.7")
    assert P.release_series("2") == (2, 0)
    assert P.release_series(None) is None and P.release_series("0.0.0-dev") is None


@pytest.mark.parametrize("version,outputs,expected", [
    ("1.2.3", [], False),                             # a platform on 1.2 goes through 1.3 first (D6)
    ("1.3.0rc1", [], True),
    ("1.3.2", [], True),
    ("1.4.0", [], True),
    ("1.2.3", [P.OUTPUT_START_RUN], False),           # the version decides when the stack carries one
    (None, [], False),                                # sam deploy carries none: 1.2 has no such output
    (None, [P.OUTPUT_START_RUN], True),
    ("0.0.0-dev", [P.OUTPUT_START_RUN], True),        # a local build's version names no release
])
def test_polyris_up_updates_from_1_3_only(version, outputs, expected):
    assert P.upgrades(version, outputs) is expected


@pytest.mark.parametrize("deployed,installing,refused", [
    ("1.4.0", "1.3.2", True),            # below 1.4: no flag allows it (D8)
    ("1.5.1", "1.3.0rc1", True),
    ("1.4.1", "1.4.0", False),           # a rollback within 1.4 is a rollback (--allow-downgrade)
    ("1.3.2", "1.3.1", False),           # a platform that never ran 1.4
    ("1.3.2", "1.4.0", False),           # the upgrade
    (None, "1.3.2", False),              # unknown versions never block
    ("1.4.0", "0.0.0-dev", False),
])
def test_no_platform_goes_back_below_1_4(deployed, installing, refused):
    assert P.rolls_back_below_floor(deployed, installing) is refused


@pytest.mark.parametrize("name,valid", [("acme-dev", True), ("polyris-dev", True), ("acme-polyris-dev", False),
                                        ("x-polyris-", False)])
def test_a_new_platforms_name_never_has_the_form_of_a_pipelines_stack(name, valid):
    assert (P.new_platform_name_errors(name) == []) is valid


# ── The scheduler roles of 1.3's pipelines (§4.1) ───────────────────────────

MACHINE = "arn:aws:states:eu-central-1:123456789012:stateMachine:acme-dev-polyris-orders"
TRUST = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "scheduler.amazonaws.com"},
                                                 "Action": "sts:AssumeRole"}]}
START = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "states:StartExecution",
                                                 "Resource": MACHINE}]}


def _role(**over) -> P.SchedulerRole:
    """The role as polyris-deploy of 1.3 made it, with ``over`` changed."""
    fields = dict(arn="arn:aws:iam::123456789012:role/acme-dev-polyris-orders-scheduler-role", path="/", trust=TRUST,
                  inline={"InvokePipelineStateMachine": START}, attached=[], stack="acme-dev-polyris-orders",
                  logical_id="PipelineSchedulerRole", machine=MACHINE)
    fields.update(over)
    return P.SchedulerRole(**fields)


@pytest.mark.parametrize("arn,covered", [
    ("arn:aws:iam::1:role/acme-dev-polyris-orders-scheduler-role", True),
    ("arn:aws:iam::1:role/some/path/acme-dev-polyris-orders-scheduler-role", True),
    ("arn:aws:iam::1:role/acme-dev-polyris-schedule-role", False),
    ("arn:aws:iam::1:role/acme-scheduler-role", False),
])
def test_the_roles_the_migration_grant_covers(arn, covered):
    assert P.is_scheduler_role(arn) is covered


def test_the_role_polyris_deploy_made_has_no_problem():
    assert P.scheduler_role_problems(_role()) == []
    single = {"Statement": {"Effect": "Allow", "Principal": {"Service": ["scheduler.amazonaws.com"]},
                            "Action": ["sts:AssumeRole"]}}
    assert P.scheduler_role_problems(_role(trust=single)) == [], "one statement, or lists of one, are the same"


@pytest.mark.parametrize("over,problem", [
    ({"stack": None, "logical_id": None}, "no pipeline stack made it"),
    ({"stack": "my-app"}, "no pipeline stack made it"),
    ({"logical_id": "SomethingElse"}, "no pipeline stack made it"),
    ({"path": "/admin/"}, "its path is /admin/"),
    ({"trust": {"Statement": [TRUST["Statement"][0], {"Effect": "Allow", "Principal": {"AWS": "*"},
                                                      "Action": "sts:AssumeRole"}]}}, "it trusts more than the scheduler"),
    ({"trust": {"Statement": [{**TRUST["Statement"][0], "Principal": {"AWS": "arn:aws:iam::9:root"}}]}},
     "it trusts more than the scheduler"),
    ({"trust": {"Statement": [{**TRUST["Statement"][0], "Condition": {"Bool": {"x": "y"}}}]}},
     "it trusts more than the scheduler"),
    ({"trust": "not a document"}, "it trusts more than the scheduler"),
    ({"attached": ["arn:aws:iam::aws:policy/AdministratorAccess"]},
     "managed policies are attached to it (arn:aws:iam::aws:policy/AdministratorAccess)"),
    ({"inline": {}}, "its inline policies do more than start its pipeline's state machine"),
    ({"inline": {"a": START, "b": START}}, "its inline policies do more than start its pipeline's state machine"),
    ({"inline": {"a": {"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}}},
     "its inline policies do more than start its pipeline's state machine"),
    ({"inline": {"a": {"Statement": [{**START["Statement"][0], "Resource": "*"}]}}},
     "its inline policies do more than start its pipeline's state machine"),
    ({"machine": None}, "its inline policies do more than start its pipeline's state machine"),
])
def test_a_role_that_differs_from_what_polyris_deploy_made_says_how(over, problem):
    assert problem in P.scheduler_role_problems(_role(**over))
