"""Task variable schema — re-exported from the single source of truth.

The canonical registry now lives in ``polyris/variables.py`` (name + JSONata
expression + kind + description), and the run_task ``$dateVars`` block is generated
from it by ``polyris.codegen.sync_variables``. This module preserves the historical
``task_variables`` API (``TASK_VARIABLES`` + the ``get_*`` helpers) for the
console_api, deriving it from that one registry — so there is a single place to edit
and no drift.

To add a variable: edit ``polyris/variables.py`` and run ``make generate-variables``.
"""
from polyris.variables import VARIABLES

# Historical shape: {name: {"source": "jsonata", "description": ...}}. Every variable
# is JSONata-derived in Prepare_Task_Input (the Python/flag builders were removed in
# v0.78, ADR #51), so every entry's source is "jsonata".
TASK_VARIABLES = {
    name: {"source": "jsonata", "description": spec["description"]}
    for name, spec in VARIABLES.items()
}


def get_jsonata_vars():
    """Variable names computed by JSONata in Prepare_Task_Input."""
    return {k for k, v in TASK_VARIABLES.items() if v["source"] == "jsonata"}


def get_python_vars():
    """Empty set — Python builder removed in v0.78 (ADR #51). No-op for compat."""
    return set()


def get_flag_vars():
    """Empty set — flag builder removed in v0.78 (ADR #51). No-op for compat."""
    return set()


def get_backfill_vars():
    """Variables propagated into task vars from SFN input (per ADR #51)."""
    return {"current_date", "PARTITION_ARG", "partition_key", "backfill_id", "is_backfill"}
