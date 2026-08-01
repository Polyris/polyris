"""
DAG class for SFN-DSL.
"""

from typing import List, Optional, Dict, Any, Callable, TYPE_CHECKING
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .constants import SCHEDULE_PRESETS
from .context import get_current_dag, push_dag_context, pop_dag_context

if TYPE_CHECKING:
    from .task import Task, TaskInstance
    from .task_group import TaskGroup
    from .steps import Step


@dataclass
class DAG:
    """
    Directed Acyclic Graph.

    Example:
        with DAG(
            dag_id="my-etl",
            schedule="@daily",
            start_date=datetime(2025, 1, 1),
            catchup=False,
            default_args={"retries": 2},
            tags=["production"],
        ) as dag:
            ...
    
    Alerts (Slack / PagerDuty) are configured in the Console UI
    (Settings → Alerts), not in the DSL (ADR #103).
    """
    dag_id: str
    description: str = ""
    
    # Scheduling
    # Can be:
    #   - str: "@daily", "@hourly", "0 * * * *" (time-based)
    #   - Asset: schedule=my_asset (triggered when asset is updated)
    #   - List[Asset]: schedule=[asset_a, asset_b] (AND - all must be ready)
    #   - AssetAll: schedule=[asset_a & asset_b] (explicit AND)
    #   - AssetAny: schedule=[asset_a | asset_b] (OR - any triggers)
    schedule: Any = None
    schedule_interval: Optional[str] = None  # Legacy alias for schedule
    timetable: Any = None  # Custom timetable
    trigger_assets: Optional[List[Any]] = None  # Alternative to schedule for asset triggers
    trigger_mode: str = "all"  # "all" (AND) or "any" (OR) for trigger_assets
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    catchup: bool = True
    
    # Default args for tasks
    default_args: Dict[str, Any] = field(default_factory=dict)
    default_timeout: Optional[int] = None  # Default timeout for all tasks (seconds)
    
    # Params
    params: Dict[str, Any] = field(default_factory=dict)
    
    # Metadata
    tags: List[str] = field(default_factory=list)
    group: str = ""  # Pipeline group for sidebar grouping (e.g. "acme", "shopmart")
    owner: str = "polyris"
    
    # Rendering
    template_searchpath: Optional[List[str]] = None
    template_undefined: Any = None
    render_template_as_native_obj: bool = False
    
    # Behavior
    max_active_tasks: int = 16
    max_active_runs: int = 16
    dagrun_timeout: Optional[timedelta] = None
    
    # SLA
    sla_miss_callback: Optional[Callable] = None
    
    # Access control
    access_control: Optional[Dict] = None
    is_paused_upon_creation: Optional[bool] = None
    
    # Documentation
    doc_md: str = ""
    
    # Pipeline variables - computed at start, available to all tasks
    variables: Dict[str, str] = field(default_factory=dict)
    
    # Internal
    tasks: List['Task'] = field(default_factory=list, repr=False)
    steps: List['Step'] = field(default_factory=list, repr=False)  # Non-task steps
    _task_instances: List['TaskInstance'] = field(default_factory=list, repr=False)
    _current_task_group: Optional['TaskGroup'] = field(default=None, repr=False)
    _eventbridge_schedule: Optional[str] = field(default=None, repr=False)
    _asset_schedule: Any = field(default=None, repr=False)  # Normalized asset schedule
    
    def __post_init__(self):
        # Handle schedule_interval alias
        if self.schedule_interval and not self.schedule:
            self.schedule = self.schedule_interval
        
        # Check if schedule is asset-based
        from .assets import normalize_asset_schedule

        # Handle trigger_assets as alternative to schedule for asset triggers
        if self.trigger_assets is not None and self.schedule is None:
            # Convert trigger_assets to schedule based on trigger_mode
            if self.trigger_mode == "any":
                from .assets import AssetAny as _AssetAny
                self.schedule = _AssetAny(self.trigger_assets)
            else:  # "all" is default
                from .assets import AssetAll as _AssetAll
                self.schedule = _AssetAll(self.trigger_assets)

        # normalize_asset_schedule is the single source of truth for "does
        # this schedule value mean asset-triggered" (Asset, AssetRef,
        # AssetConsecutiveRef, AssetAll, AssetAny, AssetAlias, and lists
        # mixing any of them, in any position). A prior version of this
        # check duplicated a narrower type-test here (Asset/AssetAll/AssetAny
        # only, and only at schedule[0] for lists) — it silently missed
        # AssetRef/AssetConsecutiveRef entirely, so `schedule=asset.within(
        # hours=1)` or `schedule=[asset.within(hours=1), other_asset]`
        # deployed with is_asset_triggered=False and _eventbridge_schedule=
        # None: no trigger mechanism at all, not time-based, not
        # asset-triggered — the pipeline simply never ran automatically,
        # with no error anywhere to reveal why.
        normalized = normalize_asset_schedule(self.schedule) if not isinstance(self.schedule, str) else None
        is_asset_based = normalized is not None

        if is_asset_based:
            # Asset-triggered DAG - no cron schedule
            self._asset_schedule = normalized
            self._eventbridge_schedule = None
        elif isinstance(self.schedule, str):
            # Time-based schedule
            if self.schedule in SCHEDULE_PRESETS:
                self._eventbridge_schedule = SCHEDULE_PRESETS[self.schedule]
            else:
                self._eventbridge_schedule = self.schedule
    
    @property
    def is_asset_triggered(self) -> bool:
        """Check if this DAG is triggered by assets (not time-based)."""
        return self._asset_schedule is not None
    
    @property
    def asset_schedule_info(self) -> Optional[Dict[str, Any]]:
        """Get asset schedule information for EventBridge rule generation."""
        if self._asset_schedule is None:
            return None
        return self._asset_schedule.to_dict()
    
    @classmethod
    def get_current_context(cls) -> Optional['DAG']:
        """Get current DAG from context stack."""
        return get_current_dag()
    
    def __enter__(self) -> 'DAG':
        """Enter DAG context."""
        push_dag_context(self)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit DAG context."""
        pop_dag_context()
    
    def add_task(self, task: 'Task'):
        """Add a task to the DAG."""
        if task not in self.tasks:
            self.tasks.append(task)
            task._dag = self
            
            # Add to current task group if in one
            if self._current_task_group:
                self._current_task_group.add_task(task)
    
    def add_step(self, step: 'Step'):
        """Add a non-task step (Wait, Pass, Choice) to the DAG."""
        if step not in self.steps:
            self.steps.append(step)
            step._dag = self
    
    def _add_task_instance(self, instance: 'TaskInstance'):
        """Track task instance."""
        if instance not in self._task_instances:
            self._task_instances.append(instance)
    
    @property
    def task_dict(self) -> Dict[str, 'Task']:
        """Get tasks as dict."""
        return {t.task_id: t for t in self.tasks}
    
    def get_task(self, task_id: str) -> Optional['Task']:
        """Get task by ID."""
        return self.task_dict.get(task_id)
    
    def topological_sort(self) -> List['Task']:
        """
        Return tasks in topological order.
        Raises ValueError if cycle detected.
        
        Note: Only considers Task dependencies, not Step dependencies.
        Steps are inline and don't affect task ordering.
        """
        from .task import Task
        
        # States: 0 = unvisited, 1 = in progress, 2 = done
        state = {t.task_id: 0 for t in self.tasks}
        result = []
        
        def visit(task: 'Task', path: List[str]):
            if task.task_id not in state:
                raise ValueError(
                    f"Task '{task.task_id}' is referenced as a dependency but was "
                    f"not added to this DAG"
                )
            if state[task.task_id] == 2:  # Already processed
                return
            if state[task.task_id] == 1:  # Cycle detected!
                cycle_path = path[path.index(task.task_id):] + [task.task_id]
                raise ValueError(f"Cycle detected in DAG: {' -> '.join(cycle_path)}")
            
            state[task.task_id] = 1  # Mark as in progress
            path.append(task.task_id)
            
            # Only visit Task dependencies (filter out Step objects)
            for dep in task.dependencies:
                if isinstance(dep, Task):
                    visit(dep, path.copy())
            
            state[task.task_id] = 2  # Mark as done
            result.append(task)
        
        for task in self.tasks:
            if state[task.task_id] == 0:
                visit(task, [])
        
        return result
    
    def roots(self) -> List['Task']:
        """Get tasks with no Task dependencies (Steps don't count)."""
        from .task import Task
        return [t for t in self.tasks if not any(isinstance(d, Task) for d in t.dependencies)]
    
    def leaves(self) -> List['Task']:
        """Get tasks with no downstream tasks."""
        from .task import Task
        all_dep_ids = set()
        for t in self.tasks:
            for dep in t.dependencies:
                if isinstance(dep, Task):
                    all_dep_ids.add(dep.task_id)
        return [t for t in self.tasks if t.task_id not in all_dep_ids]
    
    def cli(self):
        """Generate CLI commands."""
        pass
    
    def test(self, execution_date: Optional[datetime] = None):
        """Test DAG."""
        print(f"Testing DAG: {self.dag_id}")
        for task in self.topological_sort():
            print(f"  Task: {task.task_id}")
            if task.python_callable:
                try:
                    result = task.python_callable()
                    print(f"    Result: {result}")
                except Exception as e:
                    print(f"    Error: {e}")


# Alias for backwards compatibility
Pipeline = DAG
