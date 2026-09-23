"""Bare `pip install polyris` in docs is a lie today — polyris ships from
git tags, not PyPI. Every install snippet must use the PEP 508 direct-URL
form (`polyris @ git+…@<VERSION>`), a version pin (`polyris==X`), or an
extras form (`polyris[extra] @ git+…`).

Delete this check when polyris is published to PyPI. Legitimate
meta-references (e.g. gotchas doc explaining *why* the bare form doesn't
work) can suppress on a single line via `<!-- audit-docs: skip-line -->`.
"""
from __future__ import annotations

import re

import pytest

from tests.docs._helpers import (
    format_findings,
    has_suppress,
    iter_docs,
    iter_lines,
    rel,
)


_PIP_INSTALL_RE = re.compile(r"pip\s+install\s+([^\n`#]*)")
# Reject `polyris-word`, `polyris.foo`, `polyris[extra]`, `polyris @ git+…`,
# `polyris==X` etc. — those are all legit non-PyPI-bare specs.
_BARE_POLYRIS_TOKEN_RE = re.compile(
    r"(?<![-\w.])['\"]?polyris['\"]?(?![-\w.\[]|\s*[@=~<>])"
)


def test_no_bare_pypi_install() -> None:
    findings: list[tuple[str, int, str]] = []
    for path in iter_docs():
        for lineno, line in iter_lines(path):
            if has_suppress(line):
                continue
            for install_match in _PIP_INSTALL_RE.finditer(line):
                tail = install_match.group(1)
                if _BARE_POLYRIS_TOKEN_RE.search(tail):
                    findings.append((
                        rel(path),
                        lineno,
                        'bare `pip install polyris` — use '
                        '`pip install "polyris @ git+https://github.com/Polyris/polyris@<VERSION>"`',
                    ))
                    break
    if findings:
        pytest.fail(format_findings("pypi_installs", findings))
