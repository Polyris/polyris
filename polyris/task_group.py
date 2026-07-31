"""
TaskGroup for SFN-DSL.

TaskGroup allows grouping tasks together.
"""

from typing import List, Optional, Callable, TYPE_CHECKING
from dataclasses import dataclass, field
import functools

from .context import get_current_dag

if TYPE_CHECKING:
    from .task import Task, TaskInstance
    from .dag import DAG


@dataclass
class TaskGroup:
    """
    Group tasks together.
    
    Example:
        with TaskGroup("extract") as extract_group:
            @task
            def extract_orders(): pass
            
            @task  
            def extract_customers(): pass
        
        @task
        def transform(): pass
        
        extract_group >> transform()
    """
    group_id: str
    prefix_group_id: bool = True
    parent_group: Optional['TaskGroup'] = None
    tooltip: str = ""
    ui_color: str = "#ccc"
    ui_fgcolor: str = "#000"
    
    _tasks: List['Task'] = field(default_factory=list, repr=False)
    _task_instances: List['TaskInstance'] = field(default_factory=list, repr=False)
    _dag: Optional['DAG'] = field(default=None, repr=False)
    
    def __enter__(self) -> 'TaskGroup':
        """Enter TaskGroup context."""
        self._dag = get_current_dag()
        if self._dag:
            self._dag._current_task_group = self
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit TaskGroup context."""
        if self._dag:
            self._dag._current_task_group = None
    
    def add_task(self, task: 'Task'):
        """Add task to group."""
        if task not in self._tasks:
            self._tasks.append(task)
            # Prefix task_id with group_id
            if self.prefix_group_id:
                task.task_id = f"{self.group_id}.{task.task_id}"
    
    @property
    def roots(self) -> List['Task']:
        """Tasks with no dependencies within the group."""
        return [t for t in self._tasks if not any(d in self._tasks for d in t.dependencies)]
    
    @property
    def leaves(self) -> List['Task']:
        """Tasks with no downstream tasks within the group."""
        # Collect all node_ids that are dependencies of other tasks
        all_dep_ids = set()
        for t in self._tasks:
            for dep in t.dependencies:
                all_dep_ids.add(dep.node_id)
        return [t for t in self._tasks if t.task_id not in all_dep_ids]
    
    def __rshift__(self, other):
        """group >> task - all leaves connect to other"""
        from .task import Task, TaskInstance
        
        leaves = self.leaves
        if isinstance(other, TaskGroup):
            # TaskGroup >> TaskGroup: connect leaves to roots
            for leaf in leaves:
                for root in other.roots:
                    if leaf not in root.dependencies:
                        root.dependencies.append(leaf)
            return other
        elif isinstance(other, TaskInstance):
            for leaf in leaves:
                if leaf not in other.task.dependencies:
                    other.task.dependencies.append(leaf)
            return other
        elif isinstance(other, Task):
            for leaf in leaves:
                if leaf not in other.dependencies:
                    other.dependencies.append(leaf)
            return other
        elif isinstance(other, list):
            for leaf in leaves:
                for t in other:
                    if isinstance(t, TaskInstance) and leaf not in t.task.dependencies:
                        t.task.dependencies.append(leaf)
                    elif isinstance(t, Task) and leaf not in t.dependencies:
                        t.dependencies.append(leaf)
            return other
        return NotImplemented
    
    def __lshift__(self, other):
        """group << task - other connects to all roots.

        Returns `other` (not `self`), matching __rshift__'s convention —
        same reasoning as TaskInstance.__lshift__/Task.__lshift__ (see their
        docstrings): returning `self` breaks 3+-item chains like
        `group << b << a`, silently connecting both b and a directly to the
        group's roots instead of chaining a -> b -> group (verified: this
        exact bug, reproduced with a real TaskGroup).
        """
        from .task import Task, TaskInstance
        
        roots = self.roots
        if isinstance(other, TaskGroup):
            # TaskGroup << TaskGroup: connect other's leaves to our roots
            for other_leaf in other.leaves:
                for root in roots:
                    if other_leaf not in root.dependencies:
                        root.dependencies.append(other_leaf)
            return other
        elif isinstance(other, TaskInstance):
            for root in roots:
                if other.task not in root.dependencies:
                    root.dependencies.append(other.task)
            return other
        elif isinstance(other, Task):
            for root in roots:
                if other not in root.dependencies:
                    root.dependencies.append(other)
            return other
        return NotImplemented


def task_group(
    group_id: Optional[str] = None,
    prefix_group_id: bool = True,
    tooltip: str = "",
    ui_color: str = "#ccc",
    ui_fgcolor: str = "#000",
    **kwargs
):
    """
    Decorator to create a TaskGroup.
    
    Example:
        @task_group()
        def my_group():
            @task
            def task1(): pass
            @task
            def task2(): pass
        
        my_group()
    
    Or with parameters:
        @task_group(group_id="extraction", tooltip="Extract data")
        def extract_tasks():
            ...
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **call_kwargs) -> TaskGroup:
            # Use function name as group_id if not provided
            nonlocal group_id
            if group_id is None:
                group_id_value = func.__name__
            else:
                group_id_value = group_id
            
            # Create TaskGroup
            group = TaskGroup(
                group_id=group_id_value,
                prefix_group_id=prefix_group_id,
                tooltip=tooltip or func.__doc__ or "",
                ui_color=ui_color,
                ui_fgcolor=ui_fgcolor,
            )
            
            # Enter group context and execute function
            with group:
                func(*args, **call_kwargs)
            
            return group
        
        return wrapper
    
    return decorator
