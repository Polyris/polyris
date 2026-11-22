"""Task variable registry — the single source of truth.

Each entry declares a template variable's **name**, its **JSONata expression**, and a
human description. This is the one place variables are defined; the
SFN ``Prepare_Task_Input`` ``$dateVars`` block in
``sam/sfn_templates/helpers/run_task/sfn.tpl.json`` is **generated** from this
registry by ``polyris.codegen.sync_variables`` (mirroring the enum codegen). There is
no hand-maintained copy to keep in sync.

To add a variable: add one entry below, then run ``make generate-variables``. CI's
``check-generate-variables`` fails if the template drifts from this registry.

Expressions run inside the JSONata scaffolding emitted by the codegen, where two
locals are bound from the run's logical date:

* ``$cd`` — the run's date, ``YYYY-MM-DD`` (``date`` of run_task's input, read from
  ``$states.context.Execution.Input``, whose keys are ``run_task_input.Key``)
* ``$dt`` — ``$toMillis($cd & 'T00:00:00Z')`` (that date as epoch millis)
"""
from typing import Dict, TypedDict

_DATE_FMT = "'[Y0001]-[M01]-[D01]'"  # JSONata $fromMillis format for YYYY-MM-DD


class VariableSpec(TypedDict):
    expr: str
    description: str


# NOTE: order is significant — the generated ``$dateVars`` object preserves it.
VARIABLES: Dict[str, VariableSpec] = {
    "current_date":          {"expr": "$cd",
                              "description": "YYYY-MM-DD (logical run date)"},
    "PARTITION_ARG":         {"expr": "$states.context.Execution.Input.partition_arg",
                              "description": "Alias for Hive partitioning: the run's PARTITION_ARG, or current_date"},
    "date_iso":              {"expr": "$cd",
                              "description": "YYYY-MM-DD (same as current_date)"},
    "date_compact":          {"expr": "$replace($cd, '-', '')",
                              "description": "YYYYMMDD"},
    "date_slash":            {"expr": "$substring($cd,0,4) & '/' & $substring($cd,5,2) & '/' & $substring($cd,8,2)",
                              "description": "YYYY/MM/DD"},
    "date_underscore":       {"expr": "$replace($cd, '-', '_')",
                              "description": "YYYY_MM_DD"},
    "year":                  {"expr": "$substring($cd, 0, 4)",
                              "description": "YYYY"},
    "month":                 {"expr": "$substring($cd, 5, 2)",
                              "description": "MM"},
    "day":                   {"expr": "$substring($cd, 8, 2)",
                              "description": "DD"},
    "day_of_week":           {"expr": "(['thursday','friday','saturday','sunday','monday','tuesday','wednesday'])[$floor($dt / 86400000) % 7]",
                              "description": "monday, tuesday, ... (lowercase)"},
    "previous_date":         {"expr": f"$fromMillis($dt - 86400000, {_DATE_FMT})",
                              "description": "current_date - 1 day"},
    "next_date":             {"expr": f"$fromMillis($dt + 86400000, {_DATE_FMT})",
                              "description": "current_date + 1 day"},
    "minus_7_days":          {"expr": f"$fromMillis($dt - 604800000, {_DATE_FMT})",
                              "description": "current_date - 7 days"},
    "minus_14_days":         {"expr": f"$fromMillis($dt - 1209600000, {_DATE_FMT})",
                              "description": "current_date - 14 days"},
    "minus_30_days":         {"expr": f"$fromMillis($dt - 2592000000, {_DATE_FMT})",
                              "description": "current_date - 30 days"},
    "minus_7_days_compact":  {"expr": f"$replace($fromMillis($dt - 604800000, {_DATE_FMT}), '-', '')",
                              "description": "YYYYMMDD of minus_7_days"},
    "minus_30_days_compact": {"expr": f"$replace($fromMillis($dt - 2592000000, {_DATE_FMT}), '-', '')",
                              "description": "YYYYMMDD of minus_30_days"},
    "partition_key":         {"expr": "$cd",
                              "description": "The run's partition key: the same as current_date"},
    "backfill_id":           {"expr": "$exists($states.context.Execution.Input.backfill_id) ? "
                                      "$states.context.Execution.Input.backfill_id : ''",
                              "description": "Backfill run id, or empty string"},
    "is_backfill":           {"expr": "$exists($states.context.Execution.Input.backfill_id) and "
                                      "$states.context.Execution.Input.backfill_id != ''",
                              "description": "true in a run of a backfill, which carries its backfill_id"},
    "ALLOW_UNSUCCESSFUL_SPIDER_RUN": {"expr": "'True'",
                              "description": "Legacy flag, always 'True'"},
}

__all__ = ["VARIABLES", "VariableSpec"]
