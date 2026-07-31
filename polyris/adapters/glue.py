"""AWS Glue Catalog  →  List[polyris.Column].

A one-shot fetch of a Glue table's schema, intended for the
`Asset.from_glue_table("db.t")` constructor pattern. Useful when an
existing Glue table is the source of truth and you want to mirror it in
code without typing every column manually.

Public surface:
    glue_table_to_columns(database, table, *, catalog_id=None, region=None)
        -> List[Column]

Differs from the Phase 2 on-demand sync route (ADR #43): that route is
called by the UI to compare declared-vs-Glue at view time. This adapter
runs at *deploy time* on the developer's machine to populate the declared
schema in the first place. Both share the same wire format (Glue type
strings) and the same parser (`type_from_string`).
"""

from __future__ import annotations

from typing import List, Optional, TYPE_CHECKING

import boto3

from ..schema import Column, Schema, type_from_string

if TYPE_CHECKING:
    pass


def glue_table_to_columns(
    database: str,
    table: str,
    *,
    catalog_id: Optional[str] = None,
    region: Optional[str] = None,
) -> Schema:
    """Fetch a Glue Catalog table and convert its columns into Column instances.

    Returns the union of `StorageDescriptor.Columns` and `PartitionKeys`,
    matching what users typically think of as "the schema" — partition
    columns marked with `partition_key=True`.

    Args:
        database: Glue database name.
        table: Glue table name.
        catalog_id: AWS account ID hosting the catalog (for cross-account).
            Omit for same-account lookups.
        region: AWS region. Defaults to the boto3 session default
            (env var, IAM role, or ~/.aws/config).

    Raises:
        botocore.exceptions.ClientError: bubbled up unchanged from boto3
            so callers can match on error codes (e.g. EntityNotFoundException
            when the table or database does not exist, AccessDeniedException
            when IAM is missing glue:GetTable, etc.).

    The function does not catch exceptions from boto3 — `Asset.from_glue_table`
    is invoked at deploy time on the developer's machine, and a stack trace
    surfacing IAM / table-not-found errors is the right UX. The console_api
    runtime route uses a different code path that wraps these errors for the
    UI (see ADR #43).
    """
    client = boto3.client("glue", region_name=region) if region else boto3.client("glue")

    kwargs = {"DatabaseName": database, "Name": table}
    if catalog_id:
        kwargs["CatalogId"] = catalog_id

    response = client.get_table(**kwargs)
    table_obj = response.get("Table") or {}
    sd = table_obj.get("StorageDescriptor") or {}

    out: List[Column] = []
    # Regular columns
    for col in (sd.get("Columns") or []):
        out.append(_glue_column_to_polyris(col, partition_key=False))
    # Partition columns
    for col in (table_obj.get("PartitionKeys") or []):
        out.append(_glue_column_to_polyris(col, partition_key=True))

    return out


def _glue_column_to_polyris(glue_col: dict, *, partition_key: bool) -> Column:
    """Convert a single Glue column dict into a Column.

    Glue API shape: {"Name": "x", "Type": "decimal(10,2)", "Comment": "..."}.
    All three are simple strings at this point — the type string is parsed
    via the same `type_from_string` used by ADR #42 normalization, so any
    type polyris knows how to emit it can also read back from Glue without
    a second parser implementation.
    """
    name = glue_col.get("Name")
    type_str = glue_col.get("Type")
    if not name:
        raise ValueError(f"Glue column missing 'Name': {glue_col!r}")
    if not type_str:
        raise ValueError(f"Glue column {name!r} missing 'Type': {glue_col!r}")
    return Column(
        name=name,
        type=type_from_string(type_str),
        description=(glue_col.get("Comment") or "").strip(),
        partition_key=partition_key,
    )


# ──────────────────────────────────────────────────────────────────────────
# Granularity inference (ADR #50)
#
# Glue tables expose `PartitionKeys` — the columns by which the underlying
# files are physically partitioned in S3. The names of those columns are a
# strong signal of the asset's natural cadence:
#
#   year/month/day/hour → hourly
#   year/month/day      → daily
#   year/week           → weekly
#   year/month          → monthly
#
# This inference is **advisory only**. At deploy time, `polyris-deploy`
# compares the inferred granularity to the user's declaration and logs a
# warning on mismatch — but the declared value always wins. Users who know
# their data better than naming conventions imply (e.g., "year/month/day"
# folder structure but actual writes happen weekly) can override freely.
# ──────────────────────────────────────────────────────────────────────────

_HOUR_KEYS  = frozenset({"hour", "hr", "hh"})
_DAY_KEYS   = frozenset({"day", "dd", "d", "date", "dt"})
_WEEK_KEYS  = frozenset({"week", "wk", "w"})
_MONTH_KEYS = frozenset({"month", "mm", "m"})
_YEAR_KEYS  = frozenset({"year", "yy", "yyyy", "y"})


def infer_granularity_from_partition_keys(
    partition_key_names: List[str],
) -> Optional[str]:
    """Infer the asset's natural cadence from Glue PartitionKeys.

    Returns one of "hourly", "daily", "weekly", "monthly" — or None when
    the key set doesn't match any recognized convention.

    Examples:
        >>> infer_granularity_from_partition_keys(["year", "month", "day"])
        'daily'
        >>> infer_granularity_from_partition_keys(["year", "month", "day", "hour"])
        'hourly'
        >>> infer_granularity_from_partition_keys(["year", "week"])
        'weekly'
        >>> infer_granularity_from_partition_keys(["region", "year", "month", "day"])
        'daily'
    """
    lowered = {k.lower().strip() for k in partition_key_names if k}

    has_hour  = bool(lowered & _HOUR_KEYS)
    has_day   = bool(lowered & _DAY_KEYS)
    has_week  = bool(lowered & _WEEK_KEYS)
    has_month = bool(lowered & _MONTH_KEYS)
    has_year  = bool(lowered & _YEAR_KEYS)

    # Order of specificity: hourly > daily > weekly > monthly. We require
    # both the finest-grain key AND at least one coarser anchor — a lone
    # "hour" column with no date context is ambiguous.
    if has_hour and (has_day or has_year):
        return "hourly"
    if has_day and (has_month or has_year):
        return "daily"
    if has_week and has_year:
        return "weekly"
    if has_month and has_year:
        return "monthly"
    return None


def fetch_glue_partition_keys(
    database: str,
    table: str,
    *,
    catalog_id: Optional[str] = None,
    region: Optional[str] = None,
) -> List[str]:  # pragma: no cover -- thin boto3 Glue fetch; mocking only tests the mock (#11/#13/#14), exercised by deploy e2e
    """Fetch just the partition key *names* from Glue (cheaper than the full
    table-to-columns call).

    Used by `polyris-deploy` to infer granularity without parsing every
    column type. Same error semantics as `glue_table_to_columns` — boto3
    exceptions bubble up so the developer sees the real failure.
    """
    client = (
        boto3.client("glue", region_name=region) if region else boto3.client("glue")
    )
    kwargs = {"DatabaseName": database, "Name": table}
    if catalog_id:
        kwargs["CatalogId"] = catalog_id
    response = client.get_table(**kwargs)
    table_obj = response.get("Table") or {}
    return [
        (k.get("Name") or "")
        for k in (table_obj.get("PartitionKeys") or [])
        if k.get("Name")
    ]


__all__ = [
    "glue_table_to_columns",
    "infer_granularity_from_partition_keys",
    "fetch_glue_partition_keys",
]
