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

#: What a row that a 1.2 run left with no generation reads as, or with 0, which 1.2's templates wrote when their
#: input had none. Only its stop writes such a row (``row_write.LEGACY_ROW_CONDITION``, *Migration*); raised
#: generations start at ``FIRST``, so no 1.3 owner matches it.
LEGACY = 0
