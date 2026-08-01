"""
DAG Context Management.

This module provides the context stack for DAG instances,
allowing Steps and Tasks to auto-register with the current DAG.
This is separated to break circular imports between dag.py and steps.py.
"""

from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .dag import DAG

# Global context stack for DAG instances
_dag_context_stack: List['DAG'] = []


def get_current_dag() -> Optional['DAG']:
    """Get the current DAG context, if any."""
    return _dag_context_stack[-1] if _dag_context_stack else None


def push_dag_context(dag: 'DAG') -> None:
    """Push a DAG onto the context stack."""
    _dag_context_stack.append(dag)


def pop_dag_context() -> None:
    """Pop the current DAG from the context stack."""
    if _dag_context_stack:
        _dag_context_stack.pop()
