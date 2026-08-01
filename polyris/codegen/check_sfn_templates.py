"""
SFN template drift checker (v0.79.6, ADR #78).

Scans SFN template files (sam/sfn_templates/**/*.json) for status string
literals and validates each one against the canonical enums in
polyris.constants. Catches:

  1. Typos in JSONata expressions:
        $status = "skiped"  (missing 'p')  → reported as drift
  2. Status values that exist in templates but were removed from canonical.
  3. Status values added to canonical without thinking about template impact
     (informational only; templates aren't required to reference every value).

This is a CHECK, not a substitution generator. SFN templates contain
JSONata expressions that read naturally as:

    $status = "failed"

Mechanically substituting `{TASK_STATUS_FAILED}` would break readability
and complicate the SAM deploy pipeline. The drift checker enforces
consistency without touching the template syntax.

Run via: `make check-sfn-templates`

Exits 1 on any drift. Use in CI to prevent merges that silently break
SFN-to-DDB status contract.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Set, Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES_DIR = REPO_ROOT / "sam" / "sfn_templates"


def _load_canonical_status_values() -> Dict[str, Set[str]]:
    """Load canonical string values per enum family from polyris.constants."""
    from polyris.constants import (
        TaskStatus, TriggerRule, PipelineStatus, ExecutionStatus,
        BackfillStatus, BackfillCascade, BackfillGranularity,
        StalenessStatus, AssetOperator,
    )
    families: Dict[str, Set[str]] = {}
    for cls in (TaskStatus, TriggerRule, PipelineStatus, ExecutionStatus,
                BackfillStatus, BackfillCascade, BackfillGranularity,
                StalenessStatus, AssetOperator):
        values = set()
        for attr in dir(cls):
            if attr.startswith('_') or not attr.isupper():
                continue
            val = getattr(cls, attr)
            if isinstance(val, str):
                values.add(val)
        families[cls.__name__] = values
    return families


# Status-like values that templates legitimately use. The drift check
# scopes to TaskStatus (most relevant to SFN templates) — other families
# (PipelineStatus, BackfillStatus) live in DDB writes from backend, not
# in template-embedded JSONata.
def _task_status_values() -> Set[str]:
    families = _load_canonical_status_values()
    return families["TaskStatus"]


# Known short non-status words in templates that look status-y but aren't —
# allow-list to suppress false positives.
ALLOWLIST = {
    # Common JSONata / SFN keywords
    'pass', 'task', 'choice', 'wait', 'fail', 'succeed',
    'map', 'parallel', 'states', 'next', 'end',
    # Common JSON field names
    'name', 'type', 'value', 'status', 'result', 'error', 'cause',
}


# Operation-status values that helpers return as output payloads — distinct
# from task-status values that get written to the pipeline-tokens DDB
# table. These are semantically different namespaces; mixing them would
# pollute TaskStatus with values that have no meaning for task execution.
#
# Add new entries here when a helper SFN returns a new operation status.
# Each entry must be justified — these bypass canonical enforcement.
HELPER_OPERATION_STATUSES = {
    # restart_task helper returns this as its Output.status to indicate
    # the task was restarted (vs. failed-to-restart). Not a TaskStatus.
    'restarted',
}


def _extract_quoted_strings(text: str) -> List[Tuple[str, int]]:
    """Find all `"..."` string literals in template text, with line numbers."""
    results = []
    # Match double-quoted strings (handles escapes minimally)
    for m in re.finditer(r'"([a-z_][a-z0-9_]*)"', text):
        val = m.group(1)
        if val.lower() in ALLOWLIST:
            continue
        line_no = text[:m.start()].count('\n') + 1
        results.append((val, line_no))
    return results


def _looks_like_status(s: str) -> bool:
    """Heuristic: short, lowercase-with-underscore, no digits.
    Filters out filenames, ARNs, $metadata, etc."""
    if len(s) > 30 or len(s) < 4:
        return False
    if not re.match(r'^[a-z][a-z_]*$', s):
        return False
    # Skip common non-status words that pass shape but aren't statuses
    return True


def _is_near_typo(candidate: str, canonical_value: str) -> bool:
    """Loose detection: candidate looks like a typo of canonical_value.

    Returns True only if they differ by at most 1 character (substitution,
    insertion, or deletion) AND are similar length. Used to flag near-misses
    in DDB attribute values; conservative to avoid false positives.
    """
    if abs(len(candidate) - len(canonical_value)) > 1:
        return False
    if candidate == canonical_value:
        return False
    # Levenshtein-1 detection
    if len(candidate) == len(canonical_value):
        # Substitution
        diffs = sum(1 for a, b in zip(candidate, canonical_value) if a != b)
        return diffs == 1
    # Insertion/deletion: longer with one char extra at any position
    short, long = (candidate, canonical_value) if len(candidate) < len(canonical_value) else (canonical_value, candidate)
    for i in range(len(long)):
        if short == long[:i] + long[i + 1:]:
            return True
    return False


def check_templates() -> int:
    """Returns 0 if clean, 1 if any drift found."""
    canonical = _task_status_values()

    # Likely status candidates: words that look like statuses but aren't in canonical
    # AND aren't in known-non-status allowlist. We tighten by only flagging
    # words that appear inside a JSONata comparison context (= "value", ≠ "value").
    drift_findings: Dict[str, List[Tuple[Path, int]]] = {}

    template_files = sorted(TEMPLATES_DIR.rglob("*.json"))
    if not template_files:
        print("⚠️  No SFN templates found under", TEMPLATES_DIR)
        return 0

    for tpl in template_files:
        text = tpl.read_text()
        # Find JSONata status comparisons specifically:
        #   $foo.status = "value"
        #   $foo.status != "value"
        #   $foo.status in [...]
        # Pattern: word "status" followed by = / != / in, then quoted string
        status_compare_pattern = re.compile(
            r'\.status\s*(?:=|!=|in\s*\[)\s*"([a-z_][a-z0-9_]*)"',
            re.IGNORECASE,
        )
        for m in status_compare_pattern.finditer(text):
            val = m.group(1)
            line_no = text[:m.start()].count('\n') + 1
            if val not in canonical:
                key = val
                drift_findings.setdefault(key, []).append((tpl, line_no))

        # Also: literal status writes like  "status": "value"  inside Pass states
        # (these write to DDB and must match canonical)
        write_pattern = re.compile(
            r'"status"\s*:\s*"([a-z_][a-z0-9_]*)"',
            re.IGNORECASE,
        )
        for m in write_pattern.finditer(text):
            val = m.group(1)
            line_no = text[:m.start()].count('\n') + 1
            if val not in canonical and val not in HELPER_OPERATION_STATUSES:
                drift_findings.setdefault(val, []).append((tpl, line_no))

        # DDB ExpressionAttributeValues pattern, scoped to `:status` AV only.
        # Real templates use `":status": {"S": "<value>"}` for status writes;
        # other AVs like `":nf"` or `":n"` hold diagnostic attributes that
        # have no canonical contract. Restricting to `:status` (and common
        # variants) keeps false positives low.
        ddb_status_av_pattern = re.compile(
            r'":(?:status|newstatus|s)"\s*:\s*\{\s*"S"\s*:\s*"([a-z_][a-z0-9_]*)"\s*\}',
            re.IGNORECASE,
        )
        for m in ddb_status_av_pattern.finditer(text):
            val = m.group(1)
            line_no = text[:m.start()].count('\n') + 1
            if (val not in canonical
                    and val not in HELPER_OPERATION_STATUSES
                    and val not in ALLOWLIST):
                drift_findings.setdefault(val, []).append((tpl, line_no))

    if drift_findings:
        print("❌ SFN template drift detected — values not in canonical TaskStatus:")
        for val, locs in sorted(drift_findings.items()):
            print(f"\n  {val!r}:")
            for tpl, line_no in locs:
                try:
                    display = tpl.relative_to(REPO_ROOT)
                except ValueError:
                    display = tpl
                print(f"    {display}:{line_no}")
        print(f"\nCanonical values: {sorted(canonical)}")
        print("\nFix by either:")
        print("  (a) Correcting the template to use a canonical value")
        print("  (b) Adding the new value to polyris/constants.TaskStatus")
        return 1

    # Report: which canonical values are referenced by templates (informational)
    referenced = set()
    for tpl in template_files:
        text = tpl.read_text()
        for val in canonical:
            if re.search(r'"' + re.escape(val) + r'"', text):
                referenced.add(val)
    unreferenced = canonical - referenced
    print("✅ SFN templates consistent with canonical TaskStatus")
    print(f"   Referenced: {len(referenced)}/{len(canonical)} canonical values")
    if unreferenced:
        print(f"   (Not in any template: {sorted(unreferenced)} — fine if intentional)")
    return 0


if __name__ == "__main__":
    sys.exit(check_templates())
