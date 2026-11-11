"""Template contract: mark_success wakes wait_for subscribers.

Pins the two wiring artefacts without which `mark_success` silently stops
notifying pull-waiters:

1. `ConsoleApiFunction.Environment.Variables.NOTIFY_ASSET_SUBSCRIBERS_LAMBDA_ARN`
   must reference `NotifyAssetSubscribersFunction`.
2. `ConsoleApiRolePolicy` must grant `lambda:InvokeFunction` on that same
   Lambda.

Either can be removed with no unit-test breakage inside the Python code — the
mocked `lambda_client.invoke` still passes — so CloudFormation is where the
guarantee lives.
"""

import re
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).parent.parent.parent
TEMPLATE_PATH = REPO_ROOT / "sam" / "template.yaml"


@pytest.fixture(scope="module")
def template() -> dict:
    raw = TEMPLATE_PATH.read_text()

    class _Loader(yaml.SafeLoader):
        pass

    def _tag(loader, node):
        if isinstance(node, yaml.ScalarNode):
            return {"__tag__": node.tag, "value": node.value}
        if isinstance(node, yaml.SequenceNode):
            return {"__tag__": node.tag, "value": loader.construct_sequence(node)}
        return {"__tag__": node.tag, "value": loader.construct_mapping(node)}

    for tag in ("!Ref", "!Sub", "!GetAtt", "!Join", "!ImportValue",
                "!FindInMap", "!If", "!Not", "!Equals", "!And", "!Or",
                "!Select", "!Split", "!Condition"):
        _Loader.add_constructor(tag, _tag)
    return yaml.load(raw, Loader=_Loader)


def test_console_api_has_notify_asset_subscribers_env_var(template):
    env = (
        template["Resources"]["ConsoleApiFunction"]
        ["Properties"]["Environment"]["Variables"]
    )
    tag = env.get("NOTIFY_ASSET_SUBSCRIBERS_LAMBDA_ARN")
    assert tag is not None, (
        "NOTIFY_ASSET_SUBSCRIBERS_LAMBDA_ARN missing from ConsoleApiFunction — "
        "mark_success will no longer wake wait_for subscribers"
    )
    assert tag["__tag__"] == "!GetAtt"
    # !GetAtt is written as "Resource.Attribute"; the value parses to a string
    # ("NotifyAssetSubscribersFunction.Arn") or a list, depending on the YAML
    # shorthand used. Accept both shapes.
    val = tag["value"]
    flat = val if isinstance(val, str) else ".".join(val)
    assert flat == "NotifyAssetSubscribersFunction.Arn", (
        f"expected !GetAtt NotifyAssetSubscribersFunction.Arn, got {flat!r}"
    )


def test_console_api_role_grants_invoke_on_notify_asset_subscribers(template):
    """Check the raw YAML for the two sibling lines — the only way to tell
    which Resource a given Action targets without walking the full IAM
    policy structure (which varies: inline vs managed, single vs list)."""
    raw = TEMPLATE_PATH.read_text()
    # Collapse whitespace so indentation variations do not matter, then look
    # for the Action/Resource pair as adjacent tokens.
    normalized = re.sub(r"\s+", " ", raw)
    assert "lambda:InvokeFunction Resource: !GetAtt NotifyAssetSubscribersFunction.Arn" in normalized, (
        "ConsoleApiRole no longer grants lambda:InvokeFunction on "
        "NotifyAssetSubscribersFunction — mark_success invoke will fail with "
        "AccessDenied (silently, since the call is InvocationType=Event)"
    )
