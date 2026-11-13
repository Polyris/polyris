"""Every AWS resource polyris creates carries the billing tags (ADR #132, Principle #54).

Which resource types take tags comes from the CloudFormation resource schemas
that cfn-lint ships, so a type AWS learns to tag joins the rule on its own.
The platform template is checked after the SAM transform, as CloudFormation
deploys it; pipeline templates in every shape ``polyris-deploy`` writes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from cfnlint.decode import decode
from cfnlint.schema import PROVIDER_SCHEMA_MANAGER
from cfnlint.template import Template
from cfnlint.template.transforms import Transform

from polyris import DAG, Asset, resource_tags, task
from polyris.deploy import _generate_cfn_template

# The asset-scheduled shape builds an Asset; its once-per-process API notice is not under test here.
pytestmark = pytest.mark.filterwarnings("ignore::polyris.assets.ExperimentalWarning")

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "sam" / "template.yaml"
ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:x"

#: The keys of the set; any other key on a resource is a stray (one clean set, ADR #132).
OURS = {resource_tags.NAMESPACE, resource_tags.STAGE, resource_tags.COMPONENT, resource_tags.PIPELINE}

#: Tags SAM adds to the resources it generates: lambda:createdBy, stateMachine:createdBy, httpapi:createdBy.
SAM_OWN = re.compile(r"^\w+:createdBy$")

PLATFORM = {resource_tags.NAMESPACE: {"Ref": "Namespace"}, resource_tags.STAGE: {"Ref": "Stage"},
            resource_tags.COMPONENT: resource_tags.PLATFORM_COMPONENT}


def _schema(resource_type: str) -> Dict[str, Any]:
    return PROVIDER_SCHEMA_MANAGER.get_resource_schema("us-east-1", resource_type).schema


def _tag_property(resource_type: str) -> Optional[str]:
    """The property that holds a resource's tags; None when CloudFormation cannot tag the type."""
    if resource_type.startswith("Custom::"):
        return None                                  # custom resources take no tags
    schema = _schema(resource_type)
    tagging = schema.get("tagging")
    if tagging is None:                              # older schemas say it with the property alone
        return "Tags" if "Tags" in schema.get("properties", {}) else None
    if not tagging.get("taggable"):
        return None
    return str(tagging.get("tagProperty", "/properties/Tags")).rsplit("/", 1)[-1]


def _tags(properties: Dict[str, Any], prop: str) -> Dict[str, Any]:
    body = properties.get("Body")
    if isinstance(body, dict) and "tags" in body:
        # An HTTP API imported from an OpenAPI body takes its tags from the body: SAM writes them there.
        return {t["name"]: t["x-amazon-apigateway-tag-value"] for t in body["tags"] if "x-amazon-apigateway-tag-value" in t}
    raw = properties.get(prop) or {}
    return {t["Key"]: t["Value"] for t in raw} if isinstance(raw, list) else dict(raw)


def _problems(resources: Dict[str, Dict[str, Any]], expected: Dict[str, Any]) -> List[str]:
    problems = []
    for name, resource in sorted(resources.items()):
        prop = _tag_property(resource["Type"])
        if prop is None:
            continue
        found = _tags(resource.get("Properties") or {}, prop)
        if {key: found.get(key) for key in expected} != expected:
            problems.append(f"{name} ({resource['Type']}) {prop}: {found}")
        stray = sorted(key for key in found if key not in OURS and not SAM_OWN.match(key))
        if stray:
            problems.append(f"{name} ({resource['Type']}) carries tags outside the set: {stray}")
    return problems


def _platform_resources() -> Dict[str, Dict[str, Any]]:
    template, errors = decode(str(TEMPLATE))
    assert not errors, errors
    cfn = Template(str(TEMPLATE), template, ["us-east-1"])
    assert Transform().transform(cfn) == []
    resources: Dict[str, Dict[str, Any]] = cfn.template["Resources"]
    return resources


PLATFORM_RESOURCES = _platform_resources()


def test_every_platform_resource_carries_the_billing_tags():
    problems = _problems(PLATFORM_RESOURCES, PLATFORM)
    assert not problems, "tag these resources with the platform set (Principle #54):\n" + "\n".join(problems)


def test_the_check_reaches_the_resources_sam_generates():
    """The SAM transform must have run: tags on a Serverless resource only count once SAM copied them."""
    types = {r["Type"] for r in PLATFORM_RESOURCES.values()}
    assert "AWS::Lambda::Function" in types and not any(t.startswith("AWS::Serverless::") for t in types)
    assert any(_tag_property(t) is None for t in types), "the template has types AWS cannot tag; the check must know them"


def _dag(shape: str) -> DAG:
    schedule = {"cron": "cron(0 12 * * ? *)", "asset": [Asset("s3://acme-lake/orders/")], "none": None}[shape]
    with DAG(f"orders-{shape}", schedule=schedule) as dag:
        @task.sfn(arn=ARN)
        def load():
            pass
        load()
    return dag


SHAPES = ("cron", "asset", "none")


def _pipeline_resources(shape: str) -> Dict[str, Dict[str, Any]]:
    template = _generate_cfn_template(
        _dag(shape), namespace="acme", stage="prod", region="us-east-1",
        role_arn="arn:aws:iam::000000000000:role/x", wrapper_arn=ARN,
        registry_table="registry", tokens_table="tokens", asset_subscriptions_table="asset-subs",
    )
    resources: Dict[str, Dict[str, Any]] = template["Resources"]
    return resources


@pytest.mark.parametrize("shape", SHAPES)
def test_every_pipeline_resource_carries_the_billing_tags(shape):
    expected = {resource_tags.NAMESPACE: "acme", resource_tags.STAGE: "prod",
                resource_tags.COMPONENT: resource_tags.PIPELINE_COMPONENT, resource_tags.PIPELINE: f"orders-{shape}"}
    problems = _problems(_pipeline_resources(shape), expected)
    assert not problems, "tag these resources with resource_tags.pipeline_tags (Principle #54):\n" + "\n".join(problems)


def test_the_pipeline_shapes_cover_every_resource_the_generator_writes():
    """A resource added under a new condition fails here until a shape above produces it."""
    source = (REPO / "polyris" / "deploy.py").read_text()
    written = set(re.findall(r'resources\["(\w+)"\]\s*=', source))
    produced = {name for shape in SHAPES for name in _pipeline_resources(shape)}
    assert written and produced == written


def test_a_tag_change_never_replaces_a_resource():
    """Principle #33: CloudFormation replaces a resource whose create-only property changes."""
    types = {r["Type"] for r in PLATFORM_RESOURCES.values()}
    types |= {r["Type"] for shape in SHAPES for r in _pipeline_resources(shape).values()}
    replaced = sorted(t for t in types if (prop := _tag_property(t))
                      and f"/properties/{prop}" in _schema(t).get("createOnlyProperties", []))
    assert not replaced, f"changing the tags of these types replaces the resource: {replaced}"


def test_pipeline_tags_are_the_set():
    assert resource_tags.pipeline_tags("acme", "prod", "orders") == [
        {"Key": "polyris:namespace", "Value": "acme"},
        {"Key": "polyris:stage", "Value": "prod"},
        {"Key": "polyris:component", "Value": "pipeline"},
        {"Key": "polyris:pipeline", "Value": "orders"},
    ]
