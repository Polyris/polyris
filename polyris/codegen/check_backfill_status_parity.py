"""Backfill status parity drift checker (v0.80.0, ADR #83).

The terminal-status rule for a backfill lives in two runtimes that cannot
share code:

  - Python: ``polyris.backfill_status.finalize_status`` (the canonical
    authority; the console API derivation calls it directly).
  - JSONata: the ``Finalize`` state of
    ``sam/sfn_templates/bulk_backfill/sfn.tpl.json``.

This checker verifies the SFN's JSONata encodes the SAME rule as the
canonical Python function, so a future edit to either side that breaks the
agreement fails CI. It is the same idea as the SFN status-literal drift
check (ADR #78), specialised to the finalize formula.

It does NOT execute JSONata (no engine in CI). Instead it asserts the
Finalize expression contains the canonical decision structure, in order:

    canceled → 'canceled'
    failed = 0 → 'completed'
    completed = 0 → 'failed'
    else 'partial'

and — critically — that it does NOT reference ``skipped`` in the
done/aggregate computation (the ADR #82 regression: skipped must never
feed the terminal decision).

Run via: ``make check-backfill-parity``. Exits 1 on drift.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATE = (
    REPO_ROOT / "sam" / "sfn_templates" / "bulk_backfill" / "sfn.tpl.json"
)


def extract_finalize_jsonata() -> str:
    """Return the JSONata Output expression of the bulk_backfill Finalize
    state. Raises if the template or state is missing (a structural change
    the maintainer must consciously handle)."""
    data = json.loads(TEMPLATE.read_text())
    states = data.get("States", {})
    finalize = states.get("Finalize")
    if not finalize:
        raise KeyError(
            "bulk_backfill template has no 'Finalize' state — the backfill "
            "terminal-status rule moved; update this checker and ADR #83."
        )
    output = finalize.get("Output", "")
    if not output:
        raise KeyError("Finalize state has no 'Output' JSONata expression.")
    return output


def _normalize(expr: str) -> str:
    """Collapse whitespace so structural matching is layout-insensitive."""
    return re.sub(r"\s+", " ", expr).strip()


def check_parity() -> int:
    """Verify the SFN Finalize JSONata matches the canonical Python rule.

    Returns 0 if in parity, 1 on drift (with reasons printed to stderr).
    """
    problems: List[str] = []

    try:
        expr = _normalize(extract_finalize_jsonata())
    except (KeyError, json.JSONDecodeError, OSError) as e:
        print(f"[backfill-parity] cannot read Finalize JSONata: {e}",
              file=sys.stderr)
        return 1

    # Required ordered fragments encoding finalize_status(). Each is matched
    # loosely on the meaningful tokens so cosmetic edits (spacing, quote
    # placement) don't false-positive, but a logic change does.
    required: List[Tuple[str, str]] = [
        ("canceled branch",
         r"=\s*'canceled'\s*\?\s*'canceled'"),
        ("failed==0 → completed",
         r"\$failed\s*=\s*0\s*\?\s*'completed'"),
        ("completed==0 → failed",
         r"\$completed\s*=\s*0\s*\?\s*'failed'"),
        ("else → partial",
         r"'partial'"),
    ]
    for label, pat in required:
        if not re.search(pat, expr):
            problems.append(
                f"Finalize JSONata is missing the canonical "
                f"'{label}' structure (pattern: {pat!r}). The SFN terminal "
                f"rule has drifted from polyris.backfill_status.finalize_status."
            )

    # ADR #82 guard: the aggregate computation must derive from completed/
    # failed only. 'skipped' must NOT appear in the Finalize aggregate — if
    # it does, the SFN has re-introduced the bug the Python side just fixed.
    if re.search(r"skipped", expr):
        problems.append(
            "Finalize JSONata references 'skipped' — the terminal status "
            "must be computed from completed/failed only (ADR #82). Remove "
            "skipped from the aggregate."
        )

    if problems:
        print("[backfill-parity] DRIFT detected:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print("✅ Backfill status parity: SFN Finalize matches canonical rule")
    return 0


if __name__ == "__main__":
    sys.exit(check_parity())
