"""SDK ↔ Lambda parity for schema-related code that is intentionally duplicated
across the package boundary.

The console_api Lambda does not ship with the polyris SDK package — it is
deployed as a separate zip with its own `utils.py`. Two pieces are duplicated:

  - `_COLUMN_DEFAULTS` (polyris/schema.py) and `_SCHEMA_COLUMN_DEFAULTS`
    (sam/lambdas/console_api/utils.py)
  - `dict_schema_richness(...)` defined in both modules

Drift between the two would produce a silent bug: the backend conflict
resolution would pick a different schema "winner" than the SDK contract
implies. These tests catch that drift before it ships.

If a `_COLUMN_DEFAULTS` field is added or its default changes, both copies
must move together. This test fails the build if they don't.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LAMBDA_UTILS_PATH = os.path.join(
    REPO_ROOT, 'sam', 'lambdas', 'console_api', 'utils.py'
)


# ---------------------------------------------------------------------------
# Lambda module is loaded by extracting just the symbols we need, because
# importing it normally pulls in DAL repos and a circular config import that
# only works inside the Lambda's own runtime.
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def lambda_symbols() -> Dict[str, Any]:
    """Extract `_SCHEMA_COLUMN_DEFAULTS` and `dict_schema_richness` from the
    console_api Lambda's utils.py without triggering its DAL imports.
    """
    src = open(LAMBDA_UTILS_PATH).read()
    defaults_match = re.search(r'_SCHEMA_COLUMN_DEFAULTS\s*=\s*\{[^}]+\}', src, re.DOTALL)
    func_match = re.search(r'def dict_schema_richness\([^)]*\)[^:]*:\s*\".*', src, re.DOTALL)
    assert defaults_match, "Lambda utils.py must define _SCHEMA_COLUMN_DEFAULTS"
    assert func_match, "Lambda utils.py must define dict_schema_richness(...)"

    ns: Dict[str, Any] = {'List': List, 'Dict': Dict}
    exec(defaults_match.group(0), ns)
    exec(func_match.group(0), ns)
    return {
        'defaults': ns['_SCHEMA_COLUMN_DEFAULTS'],
        'richness': ns['dict_schema_richness'],
    }


@pytest.fixture(scope='module')
def sdk_symbols() -> Dict[str, Any]:
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    from polyris.schema import _COLUMN_DEFAULTS, dict_schema_richness
    return {
        'defaults': _COLUMN_DEFAULTS,
        'richness': dict_schema_richness,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_column_defaults_match(sdk_symbols, lambda_symbols):
    """`_COLUMN_DEFAULTS` (SDK) must equal `_SCHEMA_COLUMN_DEFAULTS` (Lambda).

    A new field added to one but not the other would cause the conflict
    resolver to score that field on one side only.
    """
    assert sdk_symbols['defaults'] == lambda_symbols['defaults'], (
        "Drift detected. Update both:\n"
        "  - polyris/schema.py::_COLUMN_DEFAULTS\n"
        "  - sam/lambdas/console_api/utils.py::_SCHEMA_COLUMN_DEFAULTS\n"
        f"SDK:    {sdk_symbols['defaults']}\n"
        f"Lambda: {lambda_symbols['defaults']}"
    )


# Corpus shared between SDK and Lambda. Every entry is fed to both functions
# and the scores must agree exactly.
PARITY_CORPUS = [
    ('empty list',            []),
    ('None input',            None),
    ('plain column',          [{'name': 'x', 'type': 'bigint'}]),
    ('PK column',             [{'name': 'x', 'type': 'bigint', 'primary_key': True}]),
    ('redundant nullable=true',
                              [{'name': 'x', 'type': 'bigint', 'nullable': True}]),
    ('rich column',           [{'name': 'x', 'type': 'bigint',
                                'description': 'foo', 'nullable': False}]),
    ('default=0',             [{'name': 'x', 'type': 'bigint', 'default': 0}]),
    ('default=False',         [{'name': 'x', 'type': 'bigint', 'default': False}]),
    ('default=None explicit', [{'name': 'x', 'type': 'bigint', 'default': None}]),
    ('default=""',            [{'name': 'x', 'type': 'bigint', 'default': ""}]),
    ('description=""',        [{'name': 'x', 'type': 'bigint', 'description': ""}]),
    ('description=" "',       [{'name': 'x', 'type': 'bigint', 'description': " "}]),
    ('garbage entries',       ['s', None, 42, {'name': 'ok', 'type': 'string'}]),
    ('all constraints set',   [{'name': 'x', 'type': 'bigint',
                                'primary_key': True, 'partition_key': True,
                                'nullable': False, 'unique': True,
                                'description': 'd', 'default': 0}]),
    ('mixed schema',          [
                                  {'name': 'a', 'type': 'bigint',
                                   'primary_key': True, 'nullable': False},
                                  {'name': 'b', 'type': 'date',
                                   'partition_key': True},
                                  {'name': 'c', 'type': 'string'},
                              ]),
]


@pytest.mark.parametrize('label,case', PARITY_CORPUS, ids=[c[0] for c in PARITY_CORPUS])
def test_richness_parity(sdk_symbols, lambda_symbols, label, case):
    """SDK and Lambda `dict_schema_richness` must score identically.

    This is the function that decides which schema wins when the same
    asset is declared with different schemas in multiple pipelines.
    Scoring drift = backend silently picking a different winner than the
    SDK contract implies.
    """
    sdk_score = sdk_symbols['richness'](case)
    lambda_score = lambda_symbols['richness'](case)
    assert sdk_score == lambda_score, (
        f"Drift on {label!r}: sdk={sdk_score} lambda={lambda_score}\n"
        f"input: {case!r}"
    )
