"""Resource counts and names in the docs match sam/template.yaml.

Hand-kept counts drifted more than once: a stale "118 resources", Lambda and
log-group counts that missed the coordination functions, rows for deleted
roles, and log groups named `/aws/vendedlogs/states/...` that never existed.
Recount from the template — this test fails until the docs agree with it.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("build_platform_release",
                                               REPO_ROOT / "scripts" / "build_platform_release.py")
assert _spec and _spec.loader
build = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("build_platform_release", build)
_spec.loader.exec_module(build)

TEMPLATE = build.load_cfn_yaml((REPO_ROOT / "sam" / "template.yaml").read_text())
RESOURCES: Dict[str, dict] = TEMPLATE["Resources"]
TYPES = Counter(r["Type"] for r in RESOURCES.values())

STATE_MACHINES = TYPES["AWS::Serverless::StateMachine"] + TYPES["AWS::StepFunctions::StateMachine"]
TEST_STATE_MACHINES = sum(1 for name, r in RESOURCES.items()
                          if r["Type"].endswith("::StateMachine") and name.startswith("Test"))
FUNCTIONS = TYPES["AWS::Serverless::Function"]
TABLES = TYPES["AWS::DynamoDB::Table"]
BUCKETS = TYPES["AWS::S3::Bucket"]
LOG_GROUPS = TYPES["AWS::Logs::LogGroup"]
ROLES = TYPES["AWS::IAM::Role"]
MANAGED_POLICIES = TYPES["AWS::IAM::ManagedPolicy"]

WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen".split())}

#: (doc, pattern, expected numbers for the pattern's groups). Each pattern must match exactly once.
COUNTS: List[Tuple[str, str, Tuple[int, ...]]] = [
    ("docs/deployment/INFRASTRUCTURE.md", r"### Step Functions state machines \((\d+)\)", (STATE_MACHINES,)),
    ("docs/deployment/INFRASTRUCTURE.md", r"### Lambda functions \((\d+)\)", (FUNCTIONS,)),
    ("docs/deployment/INFRASTRUCTURE.md", r"### DynamoDB tables \((\d+)\)", (TABLES,)),
    ("docs/deployment/INFRASTRUCTURE.md", r"### S3 buckets \((\d+)\)", (BUCKETS,)),
    ("docs/deployment/INFRASTRUCTURE.md", r"### CloudWatch log groups \((\d+)\)", (LOG_GROUPS,)),
    ("docs/deployment/INFRASTRUCTURE.md", r"### IAM \((\d+) roles \+ (\d+) managed policies\)",
     (ROLES, MANAGED_POLICIES)),
    ("docs/deployment/INFRASTRUCTURE.md", r"\[Lambda functions \((\d+)\)\]\(#lambda-functions-(\d+)\)",
     (FUNCTIONS, FUNCTIONS)),
    ("docs/deployment/INFRASTRUCTURE.md", r"\[CloudWatch log groups \((\d+)\)\]\(#cloudwatch-log-groups-(\d+)\)",
     (LOG_GROUPS, LOG_GROUPS)),
    ("docs/deployment/INFRASTRUCTURE.md",
     r"\[IAM \((\d+) roles \+ (\d+) managed policies\)\]\(#iam-(\d+)-roles--(\d+)-managed-policies\)",
     (ROLES, MANAGED_POLICIES, ROLES, MANAGED_POLICIES)),
    ("docs/deployment/INFRASTRUCTURE.md",
     r"exactly these — (\d+) state machines, (\d+) Lambdas, (\d+) DDB tables, (\d+) S3 buckets",
     (STATE_MACHINES, FUNCTIONS, TABLES, BUCKETS)),
    ("docs/deployment/IAM_PERMISSIONS.md", r"\| (\d+) roles \+ (\d+) managed policies \|", (ROLES, MANAGED_POLICIES)),
    ("docs/deployment/IAM_PERMISSIONS.md", r"\| (\d+) tables \(", (TABLES,)),
    ("docs/deployment/IAM_PERMISSIONS.md", r"\| (\d+) Lambda functions \(", (FUNCTIONS,)),
    ("docs/deployment/IAM_PERMISSIONS.md", r"\| (\d+) state machines \(", (STATE_MACHINES,)),
    ("docs/deployment/IAM_PERMISSIONS.md", r"\| (\d+) log groups \|", (LOG_GROUPS,)),
    ("docs/architecture/BACKEND.md", r"\*\*Step Function state machines\*\* \((\d+) total\) — (\d+) orchestration \+ (\d+) test",
     (STATE_MACHINES, STATE_MACHINES - TEST_STATE_MACHINES, TEST_STATE_MACHINES)),
    ("docs/architecture/BACKEND.md", r"\*\*Lambda functions\*\* \((\d+)\)", (FUNCTIONS,)),
    ("docs/architecture/BACKEND.md", r"\*\*DynamoDB tables\*\* \((\d+)\)", (TABLES,)),
    ("README.md", r"# (\d+) Lambda functions", (FUNCTIONS,)),
    ("CLAUDE.md", r"### (\d+) State Machines \((\d+) orchestration \+ (\d+) test\)",
     (STATE_MACHINES, STATE_MACHINES - TEST_STATE_MACHINES, TEST_STATE_MACHINES)),
]


def _doc(path: str) -> str:
    return (REPO_ROOT / path).read_text()


def _section(path: str, heading: str) -> str:
    text = _doc(path)
    start = text.index(heading)
    end = text.find("\n#", start + len(heading))
    return text[start:] if end < 0 else text[start:end]


def _sub_name(value: object) -> str:
    """'polyris-…' part of a `!Sub "…${Namespace}-${Stage}-polyris-…"` name."""
    assert isinstance(value, dict) and "Fn::Sub" in value, value
    text = value["Fn::Sub"]
    assert isinstance(text, str), text
    return text.replace("${Namespace}-${Stage}-", "")


def _names(resource_type: str, prop: str) -> Set[str]:
    return {_sub_name(r["Properties"][prop]) for r in RESOURCES.values() if r["Type"] == resource_type}


@pytest.mark.parametrize("doc,pattern,expected", COUNTS, ids=[f"{d}:{p[:40]}" for d, p, _ in COUNTS])
def test_doc_count_matches_template(doc, pattern, expected):
    found = [tuple(int(g) for g in m.groups()) for m in re.finditer(pattern, _doc(doc))]
    assert found == [expected], f"{doc}: {pattern!r} says {found}, the template has {expected}"


@pytest.mark.parametrize("doc,pattern,expected", [
    ("docs/deployment/IAM_PERMISSIONS.md", r"^(\w+) IAM roles the stack creates", ROLES),
    ("docs/deployment/IAM_PERMISSIONS.md", r"^(\w+) AWS::IAM::ManagedPolicy resources the stack also creates",
     MANAGED_POLICIES),
])
def test_doc_count_in_words_matches_template(doc, pattern, expected):
    found = [WORDS[m.group(1).lower()] for m in re.finditer(pattern, _doc(doc), re.MULTILINE)]
    assert found == [expected], f"{doc}: {pattern!r} says {found}, the template has {expected}"


def test_iam_roles_table_lists_every_role_once():
    rows = re.findall(r"^\| `(polyris-[a-z-]+-role)` \|",
                      _section("docs/deployment/IAM_PERMISSIONS.md", "## IAM roles polyris creates"), re.MULTILINE)
    assert sorted(rows) == sorted(_names("AWS::IAM::Role", "RoleName"))


def test_lambda_table_lists_every_function_once():
    rows = re.findall(r"^\| `(polyris-[a-z-]+)` \|",
                      _section("docs/deployment/INFRASTRUCTURE.md", "### Lambda functions"), re.MULTILINE)
    assert sorted(rows) == sorted(_names("AWS::Serverless::Function", "FunctionName"))


def test_log_group_table_names_every_log_group():
    section = _section("docs/deployment/INFRASTRUCTURE.md", "### CloudWatch log groups")
    missing = []
    for name in sorted(_names("AWS::Logs::LogGroup", "LogGroupName")):
        prefix, _, suffix = name.partition("polyris")      # "/aws/states/", "-run-task-helper"
        if f"`{name}`" not in section and not (f"`{suffix}`" in section and f"`{prefix}polyris-" in section):
            missing.append(name)
    assert missing == []
    assert "vendedlogs" not in _doc("docs/deployment/INFRASTRUCTURE.md")


def test_total_resource_count_after_the_sam_transform(monkeypatch):
    translator = pytest.importorskip("samtranslator.translator.transform")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")       # the transform builds ARNs; tests run without AWS env
    template = build.load_cfn_yaml((REPO_ROOT / "sam" / "template.yaml").read_text())
    for resource in template["Resources"].values():
        for key in ("CodeUri", "DefinitionUri"):
            if isinstance(resource.get("Properties", {}).get(key), str):
                resource["Properties"][key] = "s3://bucket/key"      # the transform wants S3 locations

    class _NoManagedPolicies:
        def load(self) -> Dict[str, str]:
            return {}

    pseudo = {"AWS::Region": "us-east-1", "AWS::AccountId": "123456789012", "AWS::Partition": "aws",
              "AWS::StackName": "polyris", "AWS::URLSuffix": "amazonaws.com"}
    resources = translator.transform(template, pseudo, _NoManagedPolicies())["Resources"]
    total = len(resources)
    services = len({r["Type"].split("::")[1] for r in resources.values()})
    found = [(int(m.group(1)), int(m.group(2)))
             for m in re.finditer(r"(\d+) resources across (\d+) (?:AWS )?services", _doc("docs/deployment/IAM_PERMISSIONS.md"))]
    assert found and set(found) == {(total, services)}, f"IAM_PERMISSIONS.md says {found}; the stack has {total} resources across {services} services"
