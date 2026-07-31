"""
Helper functions for SFN-DSL.

This module contains:
- chain: Chain tasks in sequence
- cross_downstream: Set cross dependencies
- Label: Edge label for DAG visualization
- dag: Decorator to define a DAG
"""

from typing import List, Dict, Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # circular-safe: annotation-only
    from .task import TaskInstance
from datetime import datetime
import functools

from .dag import DAG


def chain(*tasks):
    """
    Chain tasks in sequence.
    
    Example:
        chain(task1, task2, task3, task4)
        # Equivalent to: task1 >> task2 >> task3 >> task4
        
        chain(task1, [task2, task3], [task4, task5], task6)
        # Creates:
        #   task1 >> task2 >> task4 >> task6
        #   task1 >> task3 >> task5 >> task6
    """
    for i in range(len(tasks) - 1):
        current = tasks[i]
        next_item = tasks[i + 1]
        
        if isinstance(current, list) and isinstance(next_item, list):
            # Pairwise: [a, b] >> [c, d] means a >> c and b >> d
            for c, n in zip(current, next_item):
                if hasattr(c, '__rshift__'):
                    c >> n
        elif isinstance(current, list):
            # [a, b] >> c
            for c_item in current:
                if hasattr(c_item, '__rshift__'):
                    c_item >> next_item
        elif isinstance(next_item, list):
            # a >> [b, c]
            if hasattr(current, '__rshift__'):
                current >> next_item
        else:
            # a >> b
            if hasattr(current, '__rshift__'):
                current >> next_item


def cross_downstream(from_tasks, to_tasks):
    """
    Set cross dependencies.
    
    Example:
        cross_downstream([op1, op2], [op3, op4])
        # Creates:
        #   op1 >> op3
        #   op1 >> op4
        #   op2 >> op3
        #   op2 >> op4
    """
    for from_task in from_tasks:
        for to_task in to_tasks:
            if hasattr(from_task, '__rshift__'):
                from_task >> to_task


class Label:
    """
    Edge label for DAG visualization.
    
    Example:
        task1 >> Label("When success") >> task2
        task2 << Label("When success") << task1
    """
    def __init__(self, label: str):
        self.label = label
        self._upstream: Optional['TaskInstance'] = None
        self._downstream: Optional['TaskInstance'] = None
    
    def __rrshift__(self, other):
        """task >> Label(...)"""
        self._upstream = other
        return self
    
    def __rshift__(self, other):
        """Label(...) >> task"""
        if self._upstream is not None:
            # Connect upstream to downstream, passing through the label
            if hasattr(self._upstream, '__rshift__'):
                self._upstream >> other
        return other

    def __rlshift__(self, other):
        """task2 << Label(...) — other is downstream of the eventual edge;
        remember it and return self so the chain continues into the next
        `<< task1`, mirroring __rrshift__'s upstream-side bookkeeping."""
        self._downstream = other
        return self

    def __lshift__(self, other):
        """Label(...) << task1 — completes the edge as `other >> downstream`,
        mirroring __rshift__'s completion of the forward chain."""
        if self._downstream is not None:
            if hasattr(other, '__rshift__'):
                other >> self._downstream
        return other
    
    def __repr__(self):
        return f"Label({self.label!r})"


# ============================================
# @dag Decorator
# ============================================

def dag(
    dag_id: Optional[str] = None,
    description: str = "",
    schedule: Optional[str] = None,
    schedule_interval: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    catchup: bool = True,
    default_args: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    tags: Optional[List[str]] = None,
    owner: str = "polyris",
    max_active_tasks: int = 16,
    max_active_runs: int = 16,
    **kwargs
):
    """
    Decorator to define a DAG.
    
    Example:
        @dag(start_date=datetime(2025, 1, 1), schedule="@daily")
        def my_etl():
            @task
            def extract(): pass
            
            @task
            def load(): pass
            
            extract() >> load()
        
        my_dag = my_etl()
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **call_kwargs) -> DAG:
            # Use function name as dag_id if not provided
            nonlocal dag_id
            if dag_id is None:
                dag_id_value = func.__name__
            else:
                dag_id_value = dag_id
            
            # Create DAG
            dag_instance = DAG(
                dag_id=dag_id_value,
                description=description or func.__doc__ or "",
                schedule=schedule,
                schedule_interval=schedule_interval,
                start_date=start_date,
                end_date=end_date,
                catchup=catchup,
                default_args=default_args or {},
                params=params or {},
                tags=tags or [],
                owner=owner,
                max_active_tasks=max_active_tasks,
                max_active_runs=max_active_runs,
            )
            
            # Enter DAG context and execute function
            with dag_instance:
                func(*args, **call_kwargs)
            
            return dag_instance
        
        return wrapper
    
    return decorator


# Alias for decorator style (lowercase)
# Both @dag and @DAG work
DAG_decorator = dag
