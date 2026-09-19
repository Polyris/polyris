"""Claims like "N Lambda functions" / "N state machines" in docs must
match the actual `sam/template.yaml` resource counts.

Already caught one live regression (README claimed 6 Lambda functions,
template had 8). Small cost, real signal — kept per Principle #17.
"""
from __future__ import annotations

import re

import pytest

from tests.docs._helpers import (
    REPO_ROOT,
    format_findings,
    iter_docs,
    iter_lines,
    rel,
)


SAM_TEMPLATE = REPO_ROOT / "sam" / "template.yaml"

# (regex, aws types that sum, human label). AWS types sum where a category
# is spread across multiple CFN types (state machines = Serverless +
# StepFunctions primitives).
_COUNT_PATTERNS = [
    (re.compile(r"(\d+)\s+(?:Step Function\s+)?state\s+machines?", re.IGNORECASE),
     ("AWS::Serverless::StateMachine", "AWS::StepFunctions::StateMachine"),
     "state machines"),
    (re.compile(r"(\d+)\s+Lambda\s+functions?", re.IGNORECASE),
     ("AWS::Serverless::Function",),
     "Lambda functions"),
    (re.compile(r"(\d+)\s+(?:DynamoDB|DDB)\s+tables?", re.IGNORECASE),
     ("AWS::DynamoDB::Table",),
     "DDB tables"),
    (re.compile(r"(\d+)\s+(?:CloudWatch\s+)?log\s+groups?", re.IGNORECASE),
     ("AWS::Logs::LogGroup",),
     "log groups"),
    (re.compile(r"(\d+)\s+IAM\s+roles?", re.IGNORECASE),
     ("AWS::IAM::Role",),
     "IAM roles"),
]


def _template_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    if not SAM_TEMPLATE.exists():
        return counts
    text = SAM_TEMPLATE.read_text()
    for m in re.finditer(r"^\s+Type:\s+(AWS::[A-Za-z:]+)\s*$", text, re.MULTILINE):
        typ = m.group(1)
        counts[typ] = counts.get(typ, 0) + 1
    return counts


def test_resource_counts_match_template() -> None:
    counts = _template_counts()
    findings: list[tuple[str, int, str]] = []
    for path in iter_docs():
        for lineno, line in iter_lines(path):
            for regex, aws_types, label in _COUNT_PATTERNS:
                for m in regex.finditer(line):
                    claimed = int(m.group(1))
                    actual = sum(counts.get(t, 0) for t in aws_types)
                    if actual == 0 or claimed == actual:
                        continue
                    findings.append((
                        rel(path),
                        lineno,
                        f"claims {claimed} {label}, sam/template.yaml has {actual}",
                    ))
    if findings:
        pytest.fail(format_findings("resource_counts", findings))
