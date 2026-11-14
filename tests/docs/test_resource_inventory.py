"""Inventory claims test_resource_counts.py cannot see must match the code.

test_resource_counts.py checks "N Lambda functions"-style phrases in docs/ and
README. This covers what that phrasing misses: counts after the noun (headings
like "### Lambda functions (7)"), counts written as words, orchestration/test
splits, the total after the SAM transform, CLAUDE.md, the free route count,
and the names in the inventory tables. Hand-kept numbers here drifted more
than once ("118 resources", rows for deleted roles, log groups under a prefix
that never existed); every number and name below comes from the code.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from collections import Counter
from typing import Dict, List, Set, Tuple

import pytest

from tests.docs._helpers import REPO_ROOT

_spec = importlib.util.spec_from_file_location("build_platform_release",
                                               REPO_ROOT / "scripts" / "build_platform_release.py")
assert _spec and _spec.loader
build = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("build_platform_release", build)
_spec.loader.exec_module(build)

TEMPLATE_PATH = REPO_ROOT / "sam" / "template.yaml"
RESOURCES: Dict[str, dict] = build.load_cfn_yaml(TEMPLATE_PATH.read_text())["Resources"]
TYPES = Counter(r["Type"] for r in RESOURCES.values())

STATE_MACHINES = TYPES["AWS::Serverless::StateMachine"] + TYPES["AWS::StepFunctions::StateMachine"]
TEST_STATE_MACHINES = sum(1 for name, r in RESOURCES.items()
                          if r["Type"].endswith("::StateMachine") and name.startswith("Test"))
ORCHESTRATION = STATE_MACHINES - TEST_STATE_MACHINES
FUNCTIONS = TYPES["AWS::Serverless::Function"]
TABLES = TYPES["AWS::DynamoDB::Table"]
BUCKETS = TYPES["AWS::S3::Bucket"]
LOG_GROUPS = TYPES["AWS::Logs::LogGroup"]
ROLES = TYPES["AWS::IAM::Role"]
MANAGED_POLICIES = TYPES["AWS::IAM::ManagedPolicy"]

WORDS = ("zero one two three four five six seven eight nine ten eleven twelve "
         "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty").split()

INFRA = "docs/deployment/INFRASTRUCTURE.md"
IAM = "docs/deployment/IAM_PERMISSIONS.md"

#: (doc, pattern, expected values of its groups). Each pattern must match exactly once.
COUNTS: List[Tuple[str, str, Tuple[int, ...]]] = [
    (INFRA, r"### Step Functions state machines \((\d+)\)", (STATE_MACHINES,)),
    (INFRA, r"### Lambda functions \((\d+)\)", (FUNCTIONS,)),
    (INFRA, r"### DynamoDB tables \((\d+)\)", (TABLES,)),
    (INFRA, r"### S3 buckets \((\d+)\)", (BUCKETS,)),
    (INFRA, r"### CloudWatch log groups \((\d+)\)", (LOG_GROUPS,)),
    (INFRA, r"### IAM \((\d+) roles \+ (\d+) managed policies\)", (ROLES, MANAGED_POLICIES)),
    (INFRA, r"\[Step Functions state machines \((\d+)\)\]\(#step-functions-state-machines-(\d+)\)",
     (STATE_MACHINES, STATE_MACHINES)),
    (INFRA, r"\[Lambda functions \((\d+)\)\]\(#lambda-functions-(\d+)\)", (FUNCTIONS, FUNCTIONS)),
    (INFRA, r"\[DynamoDB tables \((\d+)\)\]\(#dynamodb-tables-(\d+)\)", (TABLES, TABLES)),
    (INFRA, r"\[CloudWatch log groups \((\d+)\)\]\(#cloudwatch-log-groups-(\d+)\)", (LOG_GROUPS, LOG_GROUPS)),
    (INFRA, r"\[IAM \((\d+) roles \+ (\d+) managed policies\)\]\(#iam-(\d+)-roles--(\d+)-managed-policies\)",
     (ROLES, MANAGED_POLICIES, ROLES, MANAGED_POLICIES)),
    (INFRA, r"exactly these — (\d+) state machines, (\d+) Lambdas, (\d+) DDB tables, (\d+) S3 buckets",
     (STATE_MACHINES, FUNCTIONS, TABLES, BUCKETS)),
    (IAM, r"\| (\d+) roles \+ (\d+) managed policies \|", (ROLES, MANAGED_POLICIES)),
    (IAM, r"for the (\d+) IAM roles \+ (\d+) managed policies polyris creates", (ROLES, MANAGED_POLICIES)),
    (IAM, r"\| (\d+) tables \(", (TABLES,)),
    ("docs/architecture/BACKEND.md",
     r"\*\*Step Function state machines\*\* \((\d+) total\) — (\d+) orchestration \+ (\d+) test",
     (STATE_MACHINES, ORCHESTRATION, TEST_STATE_MACHINES)),
    ("docs/architecture/BACKEND.md", r"#step-functions-state-machines-(\d+)\)", (STATE_MACHINES,)),
    ("docs/architecture/BACKEND.md", r"\*\*Lambda functions\*\* \((\d+)\)", (FUNCTIONS,)),
    ("docs/architecture/BACKEND.md", r"\*\*DynamoDB tables\*\* \((\d+)\)", (TABLES,)),
    ("README.md", r"(\d+) SFN definitions \((\d+) orchestration \+ (\d+) test\)",
     (STATE_MACHINES, ORCHESTRATION, TEST_STATE_MACHINES)),
    ("CLAUDE.md", r"### (\d+) State Machines \((\d+) orchestration \+ (\d+) test\)",
     (STATE_MACHINES, ORCHESTRATION, TEST_STATE_MACHINES)),
]

#: Counts written as words at the start of a sentence: (doc, pattern, expected).
WORD_COUNTS = [
    (IAM, r"^(\w+) IAM roles the stack creates", ROLES),
    (IAM, r"^(\w+) AWS::IAM::ManagedPolicy resources the stack also creates", MANAGED_POLICIES),
]

#: Docs that state how many routes the free console serves.
FREE_ROUTE_CLAIMS = [
    ("CLAUDE.md", r"REST API \((\d+) free routes"),
    ("CLAUDE.md", r"\*\*Surface \(ADR #98, #110\):\*\* (\d+) free routes"),
    ("CONTRIBUTING.md", r"REST API \((\d+) free endpoints\)"),
    ("CONTRIBUTING.md", r"All (\d+) free console_api routes"),
    ("docs/tools/DEVELOPMENT.md", r"All (\d+) free API routes registered"),
]


def _doc(path: str) -> str:
    return (REPO_ROOT / path).read_text()


def _section(path: str, heading: str) -> str:
    text = _doc(path)
    start = text.index(heading)
    end = text.find("\n#", start + len(heading))
    return text[start:] if end < 0 else text[start:end]


def _names(resource_type: str, prop: str) -> Set[str]:
    """The 'polyris-…' part of each `!Sub "…${Namespace}-${Stage}-polyris-…"` name."""
    names = set()
    for resource in RESOURCES.values():
        if resource["Type"] != resource_type:
            continue
        value = resource["Properties"][prop]
        assert isinstance(value, dict) and isinstance(value.get("Fn::Sub"), str), value
        names.add(value["Fn::Sub"].replace("${Namespace}-${Stage}-", ""))
    return names


@pytest.mark.parametrize("doc,pattern,expected", COUNTS, ids=[f"{d}:{p[:40]}" for d, p, _ in COUNTS])
def test_count_matches_the_template(doc, pattern, expected):
    found = [tuple(int(g) for g in m.groups()) for m in re.finditer(pattern, _doc(doc))]
    assert found == [expected], f"{doc}: {pattern!r} says {found}, the template has {expected}"


@pytest.mark.parametrize("doc,pattern,expected", WORD_COUNTS)
def test_count_in_words_matches_the_template(doc, pattern, expected):
    found = [WORDS.index(m.group(1).lower()) for m in re.finditer(pattern, _doc(doc), re.MULTILINE)]
    assert found == [expected], f"{doc}: {pattern!r} says {found}, the template has {expected}"


def test_iam_roles_table_lists_every_role_once():
    rows = re.findall(r"^\| `(polyris-[a-z-]+-role)` \|", _section(IAM, "## IAM roles polyris creates"), re.MULTILINE)
    assert sorted(rows) == sorted(_names("AWS::IAM::Role", "RoleName"))


def test_lambda_table_lists_every_function_once():
    rows = re.findall(r"^\| `(polyris-[a-z-]+)` \|", _section(INFRA, "### Lambda functions"), re.MULTILINE)
    assert sorted(rows) == sorted(_names("AWS::Serverless::Function", "FunctionName"))


def test_log_group_table_names_every_log_group():
    section = _section(INFRA, "### CloudWatch log groups")
    missing = []
    for name in sorted(_names("AWS::Logs::LogGroup", "LogGroupName")):
        prefix, _, suffix = name.partition("polyris")      # "/aws/states/", "-run-task-helper"
        if f"`{name}`" not in section and not (f"`{suffix}`" in section and f"`{prefix}polyris-" in section):
            missing.append(name)
    assert missing == []
    assert "vendedlogs" not in _doc(INFRA)


def test_free_route_count_matches_the_console():
    console = REPO_ROOT / "sam" / "lambdas" / "console_api"
    env = {**os.environ, "PYTHONPATH": ".", "AWS_DEFAULT_REGION": "us-east-1"}
    result = subprocess.run([sys.executable, "-c", "import main; print(len(main.ROUTES))"],
                            cwd=console, env=env, capture_output=True, text=True, check=True)
    routes = int(result.stdout.strip().splitlines()[-1])
    claims = [(doc, int(m.group(1))) for doc, pattern in FREE_ROUTE_CLAIMS for m in re.finditer(pattern, _doc(doc))]
    assert len(claims) == len(FREE_ROUTE_CLAIMS), f"a claim moved or changed wording: {claims}"
    assert {count for _, count in claims} == {routes}, f"the free console serves {routes} routes; docs say {claims}"


def test_total_resource_count_after_the_sam_transform(monkeypatch):
    translator = pytest.importorskip("samtranslator.translator.transform")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")       # the transform builds ARNs
    template = build.load_cfn_yaml(TEMPLATE_PATH.read_text())
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
             for m in re.finditer(r"(\d+) resources across (\d+) (?:AWS )?services", _doc(IAM))]
    assert found and set(found) == {(total, services)}, (
        f"{IAM} says {found}; the stack has {total} resources across {services} services")
