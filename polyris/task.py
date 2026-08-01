"""
Task classes for SFN-DSL.

This module contains:
- TaskInstance: Result of calling a @task decorated function
- Task: A task in the pipeline
- TaskDecorator: Task decorator
- task: Singleton TaskDecorator instance
"""

from typing import List, Optional, Dict, Any, Union, Callable, Tuple, TYPE_CHECKING, TypedDict, Unpack, Mapping
from dataclasses import dataclass, field
from datetime import timedelta

from .constants import TriggerRuleLiteral, TaskTypeLiteral
from .context import get_current_dag
from .xcom import XComArg

if TYPE_CHECKING:
    from .steps import Step
    from .dag import DAG

# ============================================
# Task Instance - Result of calling a task
# ============================================

class TaskInstance:
    """
    Result of calling a @task decorated function.
    Tracks dependencies based on XComArg inputs.
    """
    def __init__(self, task: 'Task', args: Optional[Tuple] = None, kwargs: Optional[Dict] = None):
        self.task = task
        self.args = args or ()
        self.kwargs = kwargs or {}
        self._downstream: List['TaskInstance'] = []
        self._upstream: List['TaskInstance'] = []
        
        # Extract dependencies from XComArg arguments
        self._extract_dependencies()
        
        # Register with DAG
        if task._dag:
            task._dag._add_task_instance(self)
    
    def _link_upstream(self, upstream: 'TaskInstance') -> None:
        """Record a dependency edge to ``upstream`` (instance + Task level), deduped."""
        if upstream not in self._upstream:
            self._upstream.append(upstream)
        if self not in upstream._downstream:
            upstream._downstream.append(self)
        if upstream.task not in self.task.dependencies:
            self.task.dependencies.append(upstream.task)

    def _extract_dependencies(self):
        """Extract task dependencies from XComArg / TaskInstance arguments.

        Accepts an XComArg, a TaskInstance, or a list of either (other values are
        ignored). All three resolve to the upstream TaskInstance the edge points to.
        """
        def as_upstream(value) -> Optional['TaskInstance']:
            if isinstance(value, XComArg):
                return value.task_instance
            if isinstance(value, TaskInstance):
                return value
            return None

        for arg in list(self.args) + list(self.kwargs.values()):
            items = arg if isinstance(arg, list) else [arg]
            for item in items:
                upstream = as_upstream(item)
                if upstream is not None:
                    self._link_upstream(upstream)
    
    @property
    def output(self) -> XComArg:
        """Get XComArg representing this task's output."""
        return XComArg(self)
    
    @property
    def wait_before(self):
        """Proxy wait_before to underlying Task."""
        return self.task.wait_before
    
    @wait_before.setter
    def wait_before(self, value):
        """Set wait_before on underlying Task."""
        self.task.wait_before = value
    
    def __rshift__(self, other: Union['TaskInstance', List['TaskInstance']]) -> Union['TaskInstance', List['TaskInstance']]:
        """task1() >> task2() sets task2 downstream of task1"""
        # Import here to avoid circular imports
        from .task_group import TaskGroup
        from .steps import Step
        from .helpers import Label
        
        if isinstance(other, TaskGroup):
            # Connect to all roots of the group
            for root in other.roots:
                if self.task not in root.dependencies:
                    root.dependencies.append(self.task)
            return other
        elif isinstance(other, Label):
            # TaskInstance >> Label: store upstream in label and return label
            other._upstream = self
            return other
        elif isinstance(other, Step):
            # TaskInstance >> Step: add task as dependency of step
            if self.task not in other.dependencies:
                other.dependencies.append(self.task)
            return other
        elif isinstance(other, list):
            for t in other:
                if isinstance(t, Step):
                    if self.task not in t.dependencies:
                        t.dependencies.append(self.task)
                else:
                    self._set_downstream(t)
            return other
        else:
            self._set_downstream(other)
            return other
    
    def __lshift__(self, other: Union['TaskInstance', List['TaskInstance']]) -> Union['TaskInstance', List['TaskInstance']]:
        """task2() << task1() sets task2 downstream of task1.

        Two bugs fixed here:

        1. Chain-breaking: every branch previously returned `self` instead
           of `other`. For `a << b << c` (Python evaluates left-to-right as
           `(a << b) << c`), returning `self` from `a << b` means the second
           operation becomes `a << c` again — `b` is silently skipped out of
           the chain entirely. For `load << transform << extract` (a real,
           three-or-more-item chain), this silently produced `load` depending
           on BOTH `transform` and `extract` directly, with `transform` having
           no dependency on `extract` at all, and no error anywhere to
           reveal the wrong graph. `__rshift__` already returns `other` for
           exactly this reason (verified: `>>` chaining is correct); `<<` now
           matches.

        2. Type-asymmetry: every operand type __rshift__ supports (TaskGroup,
           Step, Label, list-with-Steps) must also work reversed via <<, or
           the two operators silently stop being equivalent ways to write the
           same edge. Previously only a bare TaskInstance/list-of-instances
           worked; TaskGroup, Step, and Label all raised AttributeError
           ('object has no attribute _set_downstream'), since that method
           only exists on TaskInstance.
        """
        from .task_group import TaskGroup
        from .steps import Step
        from .helpers import Label

        if isinstance(other, TaskGroup):
            # Mirror of __rshift__'s "connect to all roots": here self is
            # downstream, so connect it after all of the group's leaves.
            for leaf in other.leaves:
                if leaf not in self.task.dependencies:
                    self.task.dependencies.append(leaf)
            return other
        elif isinstance(other, Label):
            # Delegate to Label's own reverse-chain support (task2 << Label
            # << task1 completes as task1 >> task2 once both ends are known).
            return other.__rlshift__(self)
        elif isinstance(other, Step):
            # Mirror of __rshift__'s Step branch (which adds the *task* as a
            # dependency of the step): here self is downstream, so add the
            # step as a dependency of self.task instead.
            if other not in self.task.dependencies:
                self.task.dependencies.append(other)
            return other
        elif isinstance(other, list):
            for t in other:
                if isinstance(t, Step):
                    if t not in self.task.dependencies:
                        self.task.dependencies.append(t)
                else:
                    t._set_downstream(self)
            return other
        else:
            other._set_downstream(self)
            return other
    
    def __rrshift__(self, other) -> 'TaskInstance':
        """[task1(), task2()] >> task3() or Step >> task()"""
        from .steps import Step
        
        if isinstance(other, Step):  # pragma: no cover -- Step.__rshift__ handles TaskInstance directly, so `step >> task()` never falls through to here
            # Step >> TaskInstance: add step as dependency
            if other not in self.task.dependencies:
                self.task.dependencies.append(other)
            return self
        elif isinstance(other, list):
            for t in other:
                if isinstance(t, Step):
                    if t not in self.task.dependencies:
                        self.task.dependencies.append(t)
                else:
                    t._set_downstream(self)
            return self
        return NotImplemented
    
    def _set_downstream(self, task_instance: 'TaskInstance'):
        """Set a task instance as downstream."""
        if task_instance not in self._downstream:
            self._downstream.append(task_instance)
        if self not in task_instance._upstream:
            task_instance._upstream.append(self)
        if self.task not in task_instance.task.dependencies:
            task_instance.task.dependencies.append(self.task)
    
    def set_downstream(self, task_or_list):
        """Explicitly set downstream task(s)."""
        self >> task_or_list
    
    def set_upstream(self, task_or_list):
        """Explicitly set upstream task(s)."""
        self << task_or_list

# ============================================
# Task Definition
# ============================================

@dataclass
class Task:
    """
    A task in the pipeline.
    
    Supports multiple execution types via task_type:
    - sfn: Nested Step Function (default)
    - lambda: Direct Lambda invocation
    - glue: AWS Glue job
    - ecs: ECS/Fargate task
    - athena: Athena query
    """
    task_id: str
    python_callable: Optional[Callable] = None
    
    # Task type - determines how task is executed
    task_type: TaskTypeLiteral = "sfn"
    
    # === Common fields (all task types) ===
    
    # ARN mapping (for sfn/lambda)
    arn: str = ""
    
    # Cross-account execution
    role: str = "same"  # 'acq', 'etl', 'processing', 'orchestration', 'same'
    
    retries: int = 0
    retry_delay: timedelta = field(default_factory=lambda: timedelta(minutes=5))
    max_retry_delay: Optional[timedelta] = None
    retry_exponential_backoff: bool = False
    retry_jitter: bool = False
    
    execution_timeout: timedelta = field(default_factory=lambda: timedelta(hours=24))
    orchestration_timeout: Optional[timedelta] = None  # How long to wait for deps. Defaults to execution_timeout
    
    
    trigger_rule: TriggerRuleLiteral = "all_success"
    
    
    doc: str = ""
    doc_md: str = ""
    
    # Backfill behavior
    skip_on_backfill: bool = False  # Skip this task during backfill runs
    
    # SFN-specific
    wait_before: Union[int, timedelta] = 0  # Wait before starting (seconds or timedelta)
    
    # === Lambda-specific fields ===
    function_name: str = ""  # Lambda function name (if not using arn)
    payload: Optional[Dict[str, Any]] = None  # Lambda payload
    
    # === Glue-specific fields ===
    job_name: str = ""  # Glue job name
    glue_arguments: Optional[Dict[str, str]] = None  # --arg=value pairs
    allocated_capacity: Optional[int] = None  # DPUs
    worker_type: Optional[str] = None  # G.1X, G.2X, etc.
    number_of_workers: Optional[int] = None
    
    # === ECS-specific fields ===
    cluster: str = ""
    task_definition: str = ""
    launch_type: str = "FARGATE"
    subnets: Optional[List[str]] = None
    security_groups: Optional[List[str]] = None
    container_overrides: Optional[Dict[str, Any]] = None
    assign_public_ip: str = "DISABLED"  # ENABLED for Fargate in a public subnet w/o NAT
    
    # === Athena-specific fields ===
    query_string: str = ""
    database: str = ""
    output_location: str = ""
    workgroup: str = "primary"
    
    # === EMR-specific fields ===
    emr_cluster_id: str = ""
    emr_step: Optional[Dict[str, Any]] = None
    
    # === Batch-specific fields ===
    job_definition: str = ""
    job_queue: str = ""
    batch_parameters: Optional[Dict[str, str]] = None
    
    # === Asset-based orchestration ===
    # Assets this task produces (emits events when task succeeds)
    outlets: List[Any] = field(default_factory=list)  # List[Asset]
    # Assets this task consumes (for lineage tracking)
    inlets: List[Any] = field(default_factory=list)   # List[Asset]
    # Assets to wait for before execution (pull-based cross-pipeline deps)
    # Supports: Asset, AssetRef (from .within()), AssetAll, AssetAny
    wait_for: List[Any] = field(default_factory=list)  # List[Asset | AssetRef]
    
    # Internal
    # Runtime truth: mixed DAGs bridge Steps into task deps (see __rshift__).
    dependencies: List[Union['Task', 'Step']] = field(default_factory=list)
    _dag: Optional['DAG'] = field(default=None, repr=False)
    _task_instances: List[TaskInstance] = field(default_factory=list, repr=False)
    
    def __post_init__(self):
        """Register asset relationships after task creation."""
        # Register this task as producer for outlet assets
        for asset in self.outlets:
            if hasattr(asset, 'add_producer'):
                asset.add_producer(self)
        
        # Register this task as consumer for inlet assets
        for asset in self.inlets:
            if hasattr(asset, 'add_consumer'):
                asset.add_consumer(self)
    
    @property
    def node_id(self) -> str:
        """Unique identifier for this node in the DAG graph."""
        return self.task_id
    
    @property
    def timeout(self) -> int:
        """Timeout in seconds for SFN."""
        return int(self.execution_timeout.total_seconds())
    
    @property
    def orchestration_timeout_seconds(self) -> int:
        """How long wrapper waits for deps (seconds). Defaults to execution_timeout."""
        if self.orchestration_timeout is not None:
            return int(self.orchestration_timeout.total_seconds())
        return self.timeout
    
    @property
    def retry_delay_seconds(self) -> int:
        """Retry delay in seconds."""
        return int(self.retry_delay.total_seconds())

    @property
    def max_retry_delay_seconds(self):
        """Max retry backoff delay in seconds, or None if uncapped."""
        return int(self.max_retry_delay.total_seconds()) if self.max_retry_delay else None
    
    @property
    def wait_before_seconds(self) -> int:
        """Wait before in seconds (handles both int and timedelta)."""
        if isinstance(self.wait_before, timedelta):
            return int(self.wait_before.total_seconds())
        return int(self.wait_before) if self.wait_before else 0
    
    def __call__(self, *args, **kwargs) -> TaskInstance:
        """Call task like a function; returns TaskInstance for wiring dependencies."""
        instance = TaskInstance(self, args, kwargs)
        self._task_instances.append(instance)
        return instance
    
    def __rshift__(self, other):
        """task >> other - works on Task objects directly too"""
        if isinstance(other, Task):
            if self not in other.dependencies:
                other.dependencies.append(self)
            return other
        elif isinstance(other, list):
            for t in other:
                if isinstance(t, Task) and self not in t.dependencies:
                    t.dependencies.append(self)
            return other
        return NotImplemented
    
    def __lshift__(self, other):
        """task << other.

        Returns `other` (not `self`), matching __rshift__'s convention —
        see TaskInstance.__lshift__'s docstring for why: returning `self`
        breaks 3+-item chains like `c << b << a`, silently dropping `b` out
        of the chain (verified: this exact bug, at the raw-Task level).
        """
        if isinstance(other, Task):
            if other not in self.dependencies:
                self.dependencies.append(other)
            return other
        elif isinstance(other, list):
            for t in other:
                if isinstance(t, Task) and t not in self.dependencies:
                    self.dependencies.append(t)
            return other
        return NotImplemented
    
    def __rrshift__(self, other):
        """[task1, task2] >> task"""
        if isinstance(other, list):
            for t in other:
                if isinstance(t, Task) and t not in self.dependencies:
                    self.dependencies.append(t)
            return self
        return NotImplemented

# ============================================
# Task Decorator
# ============================================

class CommonTaskKwargs(TypedDict, total=False):
    """Parameters shared by every @task.<type> variant decorator (ADR #109).

    Single declaration replaces the former 7x-duplicated signature block.
    Defaults live in ONE place: TaskDecorator._create_task. Adding a common
    task parameter = add it here + to _create_task (+ Task field); every
    variant picks it up automatically.
    """
    task_id: Optional[str]
    role: str
    wait_before: int
    retries: Optional[int]
    retry_delay: Optional[timedelta]
    retry_exponential_backoff: bool
    retry_jitter: bool
    max_retry_delay: Optional[timedelta]
    execution_timeout: Optional[timedelta]
    orchestration_timeout: Optional[timedelta]
    trigger_rule: TriggerRuleLiteral
    skip_on_backfill: bool
    # Asset-based orchestration — available on every task type via **common
    # (ADR #109). These reach _create_task, the Task fields, and the generator,
    # all of which handle assets generically regardless of task_type.
    outlets: Optional[List[Any]]
    inlets: Optional[List[Any]]
    wait_for: Optional[List[Any]]

_COMMON_TASK_PARAMS = frozenset(CommonTaskKwargs.__annotations__)

def _validate_common_kwargs(decorator_name: str, common: Mapping[str, object]) -> None:
    """Strict-kwargs guard (ADR #106 D5): typos in common params raise
    immediately with the decorator's own name, instead of surfacing as a
    confusing _create_task error."""
    unknown = set(common) - _COMMON_TASK_PARAMS
    if unknown:
        raise TypeError(
            f"task.{decorator_name}() got an unexpected keyword argument "
            f"{sorted(unknown)[0]!r}"
        )

class TaskDecorator:
    """
    Task decorator with service-specific variants.
    
    IMPORTANT: Base @task is not allowed - you must use a service-specific decorator:
    
        @task.sfn(arn="${workflow_arn}")      # Nested Step Function
        @task.lambda_(function_name="...")     # Lambda invocation
        @task.glue(job_name="...")             # Glue job
        @task.ecs(cluster="...", task_definition="...")  # ECS/Fargate
        @task.athena(query_string="...", database="...")  # Athena query
        @task.emr(emr_cluster_id="...", emr_step={...})   # EMR step
        @task.batch(job_definition="...", job_queue="...") # AWS Batch
    
    All decorators share common parameters:
        - retries, retry_delay, execution_timeout
        - trigger_rule
        - wait_before (rate limiting)
        - role (cross-account execution)
    """
    
    def __call__(
        self,
        _func: Optional[Callable] = None,
        *,
        arn: Optional[str] = None,
        **kwargs
    ) -> Union[Task, Callable]:
        """
        Base @task decorator.
        
        IMPORTANT: Base @task is not allowed - you must use a service-specific decorator.
        This ensures explicit service types for better clarity and validation.
        """
        # Build helpful error message
        error_msg = (
            "Base @task decorator is not allowed. Use a service-specific decorator:\n"
            "  @task.sfn(arn='${...}')              - Step Function\n"
            "  @task.lambda_(function_name='...')   - Lambda\n"
            "  @task.glue(job_name='...')           - Glue Job\n"
            "  @task.ecs(cluster='...', task_definition='...') - ECS/Fargate\n"
            "  @task.athena(query_string='...', database='...') - Athena\n"
            "  @task.emr(emr_cluster_id='...', emr_step={...})  - EMR\n"
            "  @task.batch(job_definition='...', job_queue='...') - Batch"
        )
        
        if arn is not None:
            error_msg = (
                f"Base @task(arn=...) is not allowed. Use explicit service type:\n"
                f"  @task.sfn(arn='{arn}')  # For Step Functions"
            )
        
        raise TypeError(error_msg)
    
    def _create_task(
        self,
        _func: Optional[Callable] = None,
        *,
        task_id: Optional[str] = None,
        task_type: TaskTypeLiteral = "sfn",
        arn: Optional[str] = None,
        role: str = "same",
        wait_before: int = 0,
        retries: Optional[int] = None,
        retry_delay: Optional[timedelta] = None,
        retry_exponential_backoff: Optional[bool] = None,
        retry_jitter: Optional[bool] = None,
        max_retry_delay: Optional[timedelta] = None,
        execution_timeout: Optional[timedelta] = None,
        orchestration_timeout: Optional[timedelta] = None,
        trigger_rule: TriggerRuleLiteral = "all_success",
        doc: Optional[str] = None,
        doc_md: Optional[str] = None,
        # Service-specific fields
        function_name: str = "",
        payload: Optional[Dict[str, Any]] = None,
        job_name: str = "",
        glue_arguments: Optional[Dict[str, str]] = None,
        allocated_capacity: Optional[int] = None,
        worker_type: Optional[str] = None,
        number_of_workers: Optional[int] = None,
        cluster: str = "",
        task_definition: str = "",
        launch_type: str = "FARGATE",
        subnets: Optional[List[str]] = None,
        security_groups: Optional[List[str]] = None,
        container_overrides: Optional[Dict[str, Any]] = None,
        assign_public_ip: str = "DISABLED",
        query_string: str = "",
        database: str = "",
        output_location: str = "",
        workgroup: str = "primary",
        emr_cluster_id: str = "",
        emr_step: Optional[Dict[str, Any]] = None,
        job_definition: str = "",
        job_queue: str = "",
        batch_parameters: Optional[Dict[str, str]] = None,
        # Asset-based orchestration
        outlets: Optional[List[Any]] = None,
        inlets: Optional[List[Any]] = None,
        wait_for: Optional[List[Any]] = None,  # Assets to wait for (pull-based)
        # Backfill behavior
        skip_on_backfill: bool = False,
    ) -> Union[Task, Callable]:
        """Internal method to create Task with all parameters."""
        
        def decorator(func: Callable) -> Task:
            # Get task_id from function name if not provided
            tid = task_id or func.__name__
            
            # Get docstring
            docstring = doc or func.__doc__ or ""
            
            # Get default_args from current DAG context
            dag = get_current_dag()
            default_args = dag.default_args if dag else {}
            
            # Create Task with all fields
            t = Task(
                task_id=tid,
                python_callable=func,
                task_type=task_type,
                arn=arn or "",
                role=role,
                retries=retries if retries is not None else default_args.get('retries', 0),
                retry_delay=(
                    retry_delay if retry_delay is not None
                    else default_args.get('retry_delay', timedelta(minutes=5))
                ),
                retry_exponential_backoff=(
                    retry_exponential_backoff if retry_exponential_backoff is not None
                    else default_args.get('retry_exponential_backoff', False)
                ),
                retry_jitter=(
                    retry_jitter if retry_jitter is not None
                    else default_args.get('retry_jitter', False)
                ),
                max_retry_delay=(
                    max_retry_delay if max_retry_delay is not None
                    else default_args.get('max_retry_delay')
                ),
                execution_timeout=(
                    execution_timeout if execution_timeout is not None
                    else default_args.get('execution_timeout', timedelta(hours=24))
                ),
                orchestration_timeout=(
                    orchestration_timeout if orchestration_timeout is not None
                    else default_args.get('orchestration_timeout')
                ),
                trigger_rule=trigger_rule,
                doc=docstring,
                doc_md=doc_md or "",
                wait_before=wait_before,
                # Lambda-specific
                function_name=function_name,
                payload=payload,
                # Glue-specific
                job_name=job_name,
                glue_arguments=glue_arguments,
                allocated_capacity=allocated_capacity,
                worker_type=worker_type,
                number_of_workers=number_of_workers,
                # ECS-specific
                cluster=cluster,
                task_definition=task_definition,
                launch_type=launch_type,
                subnets=subnets,
                security_groups=security_groups,
                container_overrides=container_overrides,
                assign_public_ip=assign_public_ip,
                # Athena-specific
                query_string=query_string,
                database=database,
                output_location=output_location,
                workgroup=workgroup,
                # EMR-specific
                emr_cluster_id=emr_cluster_id,
                emr_step=emr_step,
                # Batch-specific
                job_definition=job_definition,
                job_queue=job_queue,
                batch_parameters=batch_parameters,
                # Assets
                outlets=outlets or [],
                inlets=inlets or [],
                wait_for=wait_for or [],
                # Backfill
                skip_on_backfill=skip_on_backfill,
            )
            
            # Register with current DAG
            if dag:
                dag.add_task(t)
            
            return t
        
        if _func is not None:  # pragma: no cover -- every service decorator (sfn/lambda_/glue/...) validates its required args before calling _create_task, so bare usage errors out earlier
            return decorator(_func)
        return decorator
    
    def sfn(
        self,
        _func: Optional[Callable] = None,
        *,
        arn: str,  # Required!
        **common: Unpack[CommonTaskKwargs],
    ) -> Union[Task, Callable]:
        """
        Step Function task decorator. Executes a nested Step Function.
        
        Args:
            arn: Step Function ARN (required). Can use ${var} syntax.
            outlets: List of Assets this task produces
            inlets: List of Assets this task consumes
            wait_for: List of Assets to wait for before execution (pull-based)
            skip_on_backfill: If True, task is skipped by default during backfill
        
        Example:
            @task.sfn(arn="${nested_workflow_arn}", outlets=[my_asset])
            def nested_workflow(): pass
            
            # Cross-pipeline dependency (pull-based)
            @task.sfn(arn="${proc_arn}", wait_for=[inventory_asset])
            def process(): pass
            
            # With freshness constraint
            @task.sfn(arn="${proc_arn}", wait_for=[inventory_asset.within(hours=24)])
            def process(): pass
            
            # Skip scraper during backfill
            @task.sfn(arn="${scraper_arn}", skip_on_backfill=True)
            def scraper(): pass
        """
        _validate_common_kwargs("sfn", common)
        return self._create_task(
            _func=_func,
            task_type="sfn",
            arn=arn,
            **common,
        )
    
    def lambda_(
        self,
        _func: Optional[Callable] = None,
        *,
        function_name: str = "",
        arn: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        **common: Unpack[CommonTaskKwargs],
    ) -> Union[Task, Callable]:
        """
        Lambda task decorator. Invokes Lambda directly (not via nested Step Function).
        
        Args:
            function_name: Lambda function name (required if arn not provided)
            arn: Full Lambda ARN (alternative to function_name)
            payload: Input payload for Lambda
        
        Example:
            @task.lambda_(function_name="process-data")
            def process(): pass
            
            @task.lambda_(arn="arn:aws:lambda:us-east-1:123:function:my-func")
            def my_lambda(): pass
        """
        if not function_name and not arn:
            raise ValueError("@task.lambda_ requires 'function_name' or 'arn'")
        
        _validate_common_kwargs("lambda_", common)
        return self._create_task(
            _func=_func,
            task_type="lambda",
            arn=arn,
            function_name=function_name,
            payload=payload,
            **common,
        )
    
    def glue(
        self,
        _func: Optional[Callable] = None,
        *,
        job_name: str,  # Required!
        glue_arguments: Optional[Dict[str, str]] = None,
        allocated_capacity: Optional[int] = None,
        worker_type: Optional[str] = None,
        number_of_workers: Optional[int] = None,
        **common: Unpack[CommonTaskKwargs],
    ) -> Union[Task, Callable]:
        """
        Glue job task decorator.
        
        Args:
            job_name: Glue job name (required)
            glue_arguments: Job arguments (--key=value)
            worker_type: G.1X, G.2X, etc.
            number_of_workers: Number of workers
        
        Example:
            @task.glue(job_name="etl-job", glue_arguments={"--date": "2024-01-01"})
            def etl_job(): pass
        """
        # Glue StartJobRun overrides: WorkerType + NumberOfWorkers go together,
        # and AllocatedCapacity (the deprecated DPU model) is mutually exclusive
        # with the worker pair. Enforce here (the decorator is the validation
        # boundary) rather than failing at the Glue API.
        if bool(worker_type) != bool(number_of_workers):
            raise ValueError(
                "@task.glue worker_type and number_of_workers must be set together."
            )
        if allocated_capacity is not None and (worker_type or number_of_workers):
            raise ValueError(
                "@task.glue allocated_capacity is mutually exclusive with "
                "worker_type/number_of_workers (different Glue capacity models)."
            )
        _validate_common_kwargs("glue", common)
        return self._create_task(
            _func=_func,
            task_type="glue",
            job_name=job_name,
            glue_arguments=glue_arguments,
            allocated_capacity=allocated_capacity,
            worker_type=worker_type,
            number_of_workers=number_of_workers,
            **common,
        )
    
    def ecs(
        self,
        _func: Optional[Callable] = None,
        *,
        cluster: str,  # Required!
        task_definition: str,  # Required!
        launch_type: str = "FARGATE",
        subnets: Optional[List[str]] = None,
        security_groups: Optional[List[str]] = None,
        container_overrides: Optional[Dict[str, Any]] = None,
        assign_public_ip: str = "DISABLED",
        **common: Unpack[CommonTaskKwargs],
    ) -> Union[Task, Callable]:
        """
        ECS/Fargate task decorator.
        
        Args:
            cluster: ECS cluster name (required)
            task_definition: Task definition name:revision (required)
            launch_type: FARGATE or EC2
            subnets: VPC subnets for Fargate
            security_groups: Security groups
            container_overrides: Container override config
        
        Example:
            @task.ecs(
                cluster="my-cluster",
                task_definition="my-task:1",
                container_overrides={"containerOverrides": [...]}
            )
            def container_job(): pass
        """
        # Fargate runs in an ENI and requires at least one subnet; emitting an
        # empty Subnets list fails opaquely at runTask. Enforce it here (the
        # decorator is the validation boundary). EC2 tasks may omit subnets
        # (bridge/host network mode), so this check is FARGATE-only.
        if launch_type == "FARGATE" and not subnets:
            raise ValueError(
                "@task.ecs with launch_type='FARGATE' requires subnets "
                "(Fargate tasks run in an ENI). Pass subnets=[...]."
            )
        _validate_common_kwargs("ecs", common)
        return self._create_task(
            _func=_func,
            task_type="ecs",
            cluster=cluster,
            task_definition=task_definition,
            launch_type=launch_type,
            subnets=subnets,
            security_groups=security_groups,
            container_overrides=container_overrides,
            assign_public_ip=assign_public_ip,
            **common,
        )
    
    def athena(
        self,
        _func: Optional[Callable] = None,
        *,
        query_string: str,  # Required!
        database: str,  # Required!
        output_location: str = "",
        workgroup: str = "primary",
        **common: Unpack[CommonTaskKwargs],
    ) -> Union[Task, Callable]:
        """
        Athena query task decorator.
        
        Args:
            query_string: SQL query (required)
            database: Athena database (required)
            output_location: S3 path for results
            workgroup: Athena workgroup
        
        Example:
            @task.athena(
                query_string="SELECT * FROM sales WHERE date = '{{ ds }}'",
                database="analytics",
                output_location="s3://bucket/athena-results/"
            )
            def run_query(): pass
        """
        _validate_common_kwargs("athena", common)
        return self._create_task(
            _func=_func,
            task_type="athena",
            query_string=query_string,
            database=database,
            output_location=output_location,
            workgroup=workgroup,
            **common,
        )
    
    def emr(
        self,
        _func: Optional[Callable] = None,
        *,
        emr_cluster_id: str,  # Required!
        emr_step: Dict[str, Any],  # Required!
        **common: Unpack[CommonTaskKwargs],
    ) -> Union[Task, Callable]:
        """
        EMR step task decorator.
        
        Args:
            emr_cluster_id: EMR cluster ID (required)
            emr_step: Step configuration (required)
        
        Example:
            @task.emr(
                emr_cluster_id="j-XXXXX",
                emr_step={"Name": "Spark Job", "ActionOnFailure": "CONTINUE", ...}
            )
            def spark_job(): pass
        """
        # emr_step is passed through verbatim as the addStep `Step` argument, so
        # it must be a valid StepConfig. HadoopJarStep.Jar is required by the EMR
        # API — enforce it here (the decorator is the validation boundary) rather
        # than failing opaquely at runtime with an empty Jar.
        if not isinstance(emr_step, dict):
            raise ValueError(
                f"@task.emr emr_step must be a dict (AWS StepConfig), got "
                f"{type(emr_step).__name__}."
            )
        if "Steps" in emr_step:
            raise ValueError(
                "@task.emr emr_step is a single StepConfig (addStep.sync), not a "
                "list. Pass one step, e.g. "
                "emr_step={'Name': ..., 'HadoopJarStep': {'Jar': ...}}."
            )
        if not emr_step.get("HadoopJarStep", {}).get("Jar"):
            raise ValueError(
                "@task.emr emr_step requires HadoopJarStep.Jar (e.g. "
                "'command-runner.jar' or an s3:// jar). Got HadoopJarStep="
                f"{emr_step.get('HadoopJarStep')!r}."
            )
        _validate_common_kwargs("emr", common)
        return self._create_task(
            _func=_func,
            task_type="emr",
            emr_cluster_id=emr_cluster_id,
            emr_step=emr_step,
            **common,
        )
    
    def batch(
        self,
        _func: Optional[Callable] = None,
        *,
        job_definition: str,  # Required!
        job_queue: str,  # Required!
        batch_parameters: Optional[Dict[str, str]] = None,
        **common: Unpack[CommonTaskKwargs],
    ) -> Union[Task, Callable]:
        """
        AWS Batch job task decorator.
        
        Args:
            job_definition: Batch job definition (required)
            job_queue: Batch job queue (required)
            batch_parameters: Job parameters
        
        Example:
            @task.batch(
                job_definition="my-job-def:1",
                job_queue="my-queue"
            )
            def batch_job(): pass
        """
        _validate_common_kwargs("batch", common)
        return self._create_task(
            _func=_func,
            task_type="batch",
            job_definition=job_definition,
            job_queue=job_queue,
            batch_parameters=batch_parameters,
            **common,
        )

# Create singleton instance
task = TaskDecorator()
