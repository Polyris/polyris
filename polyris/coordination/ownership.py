"""Who may write a task row: its generation (ADR #137 §3, Rule 47).

A task row has one owner at a time, named by its generation. A write made for
an owner is fenced by it (``task_row.FENCE``), and a write that ends an
ownership, a decision or a restart, raises it in the same update (the
transition table's ``raises_generation``). ``tests/sdk/test_task_row_writers.py``
checks every writer of a task row.
"""

from __future__ import annotations

#: The task-row attribute that names the current owner.
FIELD = "generation"

#: The generation a run's rows start with (``Start_Run``).
FIRST = 1
