"""Cron cadence inference for pipeline backfill partition expansion.

Per ADR #52: pipeline backfill needs to know cadence to expand date range
into partition keys (one per cron firing, not blindly one per day).

This module provides ``infer_cron_cadence(cron_string)`` that returns
``"hourly" | "daily" | "weekly" | "monthly" | None``. ``None`` means
ambiguous cron — caller falls back to ``"daily"`` with warning surfaced
in backfill preview.

Inference is best-effort. We do NOT try to support pathological cron
patterns; if pattern is irregular, return ``None`` and let the caller
handle it. See ADR #52 "edge cases" section for the supported matrix.
"""

import re
from typing import Optional

Granularity = str  # Literal["hourly", "daily", "weekly", "monthly"]

# AWS EventBridge rate() expressions — map common units to granularity.
_RATE_PATTERN = re.compile(r"^rate\(\s*(\d+)\s+(minute|minutes|hour|hours|day|days)\s*\)$")

# Cron shorthand patterns supported by some schedulers.
_CRON_SHORTHANDS = {
    "@hourly": "hourly",
    "@daily": "daily",
    "@midnight": "daily",
    "@weekly": "weekly",
    "@monthly": "monthly",
}


def _is_single_value(field: str) -> bool:
    """Field represents exactly one value (number or named day)."""
    if not field or field == "*":
        return False
    if "," in field or "-" in field or "/" in field or "?" in field:
        return False
    # Reject "L" (last-day), "#" (nth weekday-of-month) — not single-value semantically
    if "L" in field.upper() or "#" in field:
        return False
    return True


def _is_wildcard_step(field: str) -> bool:
    """Field is ``*/N`` step pattern."""
    return bool(re.match(r"^\*/\d+$", field))


def infer_cron_cadence(cron: Optional[str]) -> Optional[Granularity]:
    """Infer a backfill granularity from a cron string.

    Args:
        cron: Standard 5-field cron, AWS EventBridge ``rate(...)``,
              or ``@shorthand``. Empty or ``None`` returns ``"daily"``.

    Returns:
        ``"hourly" | "daily" | "weekly" | "monthly"`` for recognized
        patterns; ``None`` for ambiguous ones. The caller defaults
        ``None`` to ``"daily"`` and surfaces a warning.
    """
    if cron is None:
        return "daily"
    cron = cron.strip()
    if not cron:
        return "daily"

    # Shorthand: @daily, @weekly, etc.
    if cron in _CRON_SHORTHANDS:
        return _CRON_SHORTHANDS[cron]

    # AWS rate(N unit) expression
    m = _RATE_PATTERN.match(cron)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        if unit.startswith("minute"):
            return "hourly"  # sub-hourly cadence buckets as hourly
        if unit.startswith("hour"):
            return "hourly"
        if unit.startswith("day"):
            return "daily" if n == 1 else None  # rate(2 days) is not in our set
        return None  # pragma: no cover -- _RATE_PATTERN only admits minute/hour/day units, all handled above, so this fallthrough is unreachable

    parts = cron.split()
    if len(parts) != 5:
        return None  # invalid → ambiguous

    minute, hour, day_month, month, day_week = parts

    # Month must be wildcard for our four granularities (no quarterly/yearly)
    if month != "*":
        return None

    # Hourly: hour is wildcard or step
    if hour in ("*", "*/1") or _is_wildcard_step(hour):
        return "hourly"

    # From here, hour must be a single fixed value
    if not _is_single_value(hour):
        return None

    # Daily: both day fields wildcard
    if day_month == "*" and day_week == "*":
        return "daily"

    # Weekly: day-of-week single (named or numeric), day-of-month wildcard
    if day_month == "*" and _is_single_value(day_week):
        return "weekly"

    # Monthly: day-of-month single number, day-of-week wildcard
    if _is_single_value(day_month) and day_week == "*":
        return "monthly"

    # Anything else (multi-day weekdays, twice-monthly, etc.) → ambiguous
    return None
