"""Who may write a task row: its generation (ADR #130 §2.1, D40).

A task row has one owner at a time: the wrapper that holds the row's
generation, and everything that wrapper starts. Two kinds of writes keep it so:

* a write made for an owner carries ``FENCE`` in its condition, so it changes
  nothing once the owner has been replaced;
* a write that ends an ownership (an operator's decision, a restart, the
  reconciler closing a zombie) raises the generation with ``RAISE``, in the
  same update that records the decision.

The state-machine templates spell the same expressions;
``tests/sdk/test_task_row_writers.py`` checks every writer of a task row against
these constants.
"""

from __future__ import annotations

from typing import Dict

#: The task-row attribute that names the current owner.
FIELD = "generation"

#: The generation a new run's wrapper starts with.
FIRST = 1

#: What an owner input without a generation counts as: it came from a wrapper
#: started before the upgrade. Raised generations start at ``FIRST``, so such an
#: owner matches only rows that nobody has raised.
LEGACY = 0

#: Condition for a write made for the owner holding ``:g``. A row written before
#: the upgrade has no generation and stays writable until an ownership change
#: gives it one.
FENCE = "(generation = :g OR attribute_not_exists(generation))"

#: SET clause for a write that ends an ownership. Needs ``RAISE_VALUES``.
RAISE = "generation = if_not_exists(generation, :zero) + :one"

#: The values ``RAISE`` refers to.
RAISE_VALUES: Dict[str, int] = {":zero": 0, ":one": 1}


def is_legacy(row_generation: object) -> bool:
    """Whether a row is still owned by a wrapper started before the upgrade.

    Such a wrapper retries its dependency wait on a timeout; a wrapper that
    holds a raised generation never does (D41).
    """
    return row_generation in (None, "") or str(row_generation) in (str(LEGACY), f"{LEGACY}.0")
