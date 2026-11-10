"""Pre-deploy checks for referenced AWS resources.

Two checks run before CloudFormation deploy:

1. Existence check (check_resource_references)
   Scans dag.tasks for external resource references (SFN ARNs, Lambda names/ARNs,
   Glue jobs, ECS clusters/task definitions, Batch queues/job definitions, Athena
   workgroups) and verifies each exists. Aborts deploy on missing resources.

2. Override check (check_resource_overrides)
   Advisory only — never aborts deploy. For Glue, ECS, and Batch tasks, compares
   declared task parameters against the AWS resource's current defaults. Reports
   parameters that differ (potential silent runtime overrides), Glue arguments in
   NonOverridableArguments (silently dropped at runtime), and parameters that match
   defaults (can be omitted for cleaner DAGs).

   The existence check passes ResourceCheckResult.failed_refs to the override check
   so resources already confirmed broken or IAM-inaccessible are not re-attempted.
   Throttle/transient errors are excluded from failed_refs because the resource may
   still exist and the override check will surface a fresh unverified entry if it
   also can't reach the service.

Skipped silently (no output):
  - CloudFormation substitution variables (${...}) — resolved at CFN deploy time
  - emr tasks — emr_cluster_id is a runtime resource ID, not a static name

Skipped with warning:
  - Tasks using a different IAM role (task.role != "same") — the deploy session
    cannot assume that role; shown so the operator knows what was not checked.

NOTE: dag.steps are not covered in V1 — only dag.tasks are checked.
NOTE: Bulk deploy deduplicates per-DAG; the same resource in 10 DAGs = 10 checks.
      For large bulk deployments against services with low TPS quotas (Glue ~10 TPS,
      Batch ~2 TPS), throttle warnings are expected and non-blocking.
NOTE: Both checks run on --dry-run as well as normal deploys; both are skipped on
      --destroy. Full IAM requirements in ADR #126 and ADR #127; access-denied
      converts to a warning and deploy proceeds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Set, Tuple, TYPE_CHECKING

from botocore.exceptions import BotoCoreError, ClientError

from ._ansi import c as _c, BOLD_GREEN, CYAN, RED, YELLOW

if TYPE_CHECKING:
    import boto3
    from .dag import DAG


# ---- Error codes that mean "cannot verify" (not "resource missing") ----------

_CANNOT_VERIFY_CODES: frozenset[str] = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "UnauthorizedAccess",
        "AuthFailure",
        "KmsAccessDeniedException",  # SFN/Lambda with KMS encryption — missing kms:Decrypt
        "KmsThrottlingException",  # KMS throttle during describe — not a missing resource
        "KmsInvalidStateException",  # KMS key disabled/pending deletion — cannot verify
        "ThrottlingException",
        "Throttling",
        "RequestThrottled",
        "ProvisionedThroughputExceededException",
        "RequestLimitExceeded",
        "TooManyRequestsException",
        "ServiceUnavailableException",  # transient service outage — not a missing resource
        "ExpiredTokenException",  # temporary credentials expired mid-check
        "RequestExpired",  # clock-skew SigV4 failure
    }
)

# Subset of _CANNOT_VERIFY_CODES: errors where the fix is "add IAM permission".
# Used to gate the IAM hint in print_resource_check_result — throttle and
# transient errors should never prompt the operator to change their IAM policy.
_ACCESS_DENIED_CODES: frozenset[str] = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "UnauthorizedAccess",
        "AuthFailure",
    }
)


# ---- Result types ------------------------------------------------------------


@dataclass
class ResourceRef:
    """A single AWS resource reference extracted from a dag task."""

    task_id: str
    kind: str  # 'sfn','lambda','glue','ecs_cluster','ecs_task_definition',
    # 'batch_job_queue','batch_job_definition','athena_workgroup'
    ref: str  # the ARN or resource name
    role: str = "same"


@dataclass
class ResourceCheckResult:
    errors: List[ResourceRef] = field(default_factory=list)  # confirmed missing → abort
    warnings: List[ResourceRef] = field(default_factory=list)  # could not verify
    access_denied: List[ResourceRef] = field(default_factory=list)  # IAM-denied subset of warnings
    skipped: List[ResourceRef] = field(default_factory=list)  # different IAM role
    _checked: int = field(default=0, repr=False)  # refs attempted (incl. failures)
    # (kind, ref) for errors + access_denied — passed to check_resource_overrides to skip
    # resources we know are broken or inaccessible; throttle/BotoCoreError excluded because
    # the resource may still exist (transient) and the override check will surface a fresh
    # unverified entry if it also can't reach the service.
    failed_refs: Set[Tuple[str, str]] = field(default_factory=set, repr=False)
    # Set to True when _warn_no_region() fires inside check_resource_references — passed to
    # check_resource_overrides as region_warned so the warning is not printed a second time.
    region_warned: bool = field(default=False, repr=False)


# ---- Collection --------------------------------------------------------------

# task_types that are skipped entirely — no refs collected, no warnings.
_RESOURCE_SKIP_TYPES: frozenset[str] = frozenset(
    {
        "emr",  # emr_cluster_id is a runtime resource; state can change between check and run
    }
)

# Maps each non-skip task_type to the check-kinds it produces.
# When adding a new task type, update ALL SIX of these in the same change:
#   _collect_resource_refs  _TASK_TYPE_TO_KINDS  _RESOURCE_CHECKERS
#   _KIND_TO_SERVICE  _IAM_ACTION  _KIND_DISPLAY
# test_every_task_type_has_checker_or_skip enforces completeness at test time.
_TASK_TYPE_TO_KINDS: Dict[str, List[str]] = {
    "sfn": ["sfn"],
    "lambda": ["lambda"],
    "glue": ["glue"],
    "ecs": ["ecs_cluster", "ecs_task_definition"],
    "athena": ["athena_workgroup"],
    "batch": ["batch_job_queue", "batch_job_definition"],
}


def _collect_resource_refs(dag: "DAG") -> List[ResourceRef]:
    """Extract all checkable resource refs from dag.tasks.

    Does not filter cross-account or ${...} refs — caller does that.
    """
    refs: List[ResourceRef] = []
    for task in dag.tasks:
        task_type = task.task_type
        role = getattr(task, "role", "same")

        if task_type == "sfn":
            if task.arn:
                refs.append(ResourceRef(task_id=task.task_id, kind="sfn", ref=task.arn, role=role))

        elif task_type == "lambda":
            ref = task.arn or task.function_name
            if ref:
                refs.append(ResourceRef(task_id=task.task_id, kind="lambda", ref=ref, role=role))

        elif task_type == "glue":
            if task.job_name:
                refs.append(ResourceRef(task_id=task.task_id, kind="glue", ref=task.job_name, role=role))

        elif task_type == "ecs":
            if task.cluster:
                refs.append(ResourceRef(task_id=task.task_id, kind="ecs_cluster", ref=task.cluster, role=role))
            if task.task_definition:
                refs.append(
                    ResourceRef(task_id=task.task_id, kind="ecs_task_definition", ref=task.task_definition, role=role)
                )

        elif task_type == "athena":
            wg = task.workgroup
            if wg and wg != "primary":
                refs.append(ResourceRef(task_id=task.task_id, kind="athena_workgroup", ref=wg, role=role))

        elif task_type == "batch":
            if task.job_queue:
                refs.append(ResourceRef(task_id=task.task_id, kind="batch_job_queue", ref=task.job_queue, role=role))
            if task.job_definition:
                refs.append(
                    ResourceRef(task_id=task.task_id, kind="batch_job_definition", ref=task.job_definition, role=role)
                )

        elif task_type in _RESOURCE_SKIP_TYPES:
            pass  # emr_cluster_id is a runtime resource ID

        else:
            raise ValueError(
                f"task_type {task_type!r} is not recognised by the pre-deploy resource check. "
                "This is a polyris internal error — please open an issue."
            )

    return refs


# ---- Per-service check functions -------------------------------------------


class _NotFoundError(Exception):
    """Raised by check functions when an API returns an empty list instead of an exception."""


def _check_sfn(client: Any, ref: str) -> None:
    # describe_state_machine raises StateMachineDoesNotExist when the ARN is gone;
    # no status filter needed — the API throws, not returns a list with lifecycle states.
    client.describe_state_machine(stateMachineArn=ref)


def _check_lambda(client: Any, ref: str) -> None:
    # get_function raises ResourceNotFoundException when missing; no status filter needed.
    client.get_function(FunctionName=ref)


def _check_glue_job(client: Any, ref: str) -> None:
    # get_job raises EntityNotFoundException when missing; no status filter needed.
    client.get_job(JobName=ref)


def _check_ecs_cluster(client: Any, ref: str) -> None:
    resp = client.describe_clusters(clusters=[ref])
    # PROVISIONING clusters are being created and usable as a deploy target —
    # treat them the same as ACTIVE so a just-created cluster doesn't abort deploy.
    active = [c for c in resp.get("clusters", []) if c.get("status") in {"ACTIVE", "PROVISIONING"}]
    if not active:
        raise _NotFoundError(ref)


def _check_ecs_task_def(client: Any, ref: str) -> None:
    resp = client.describe_task_definition(taskDefinition=ref)
    if resp.get("taskDefinition", {}).get("status") != "ACTIVE":
        raise _NotFoundError(ref)


# Batch job queue lifecycle statuses that mean the queue is gone or broken.
# CREATING and UPDATING are intentionally NOT here — a queue in CREATING/UPDATING
# accepts job submissions (jobs queue up). Treating them as unusable would produce
# false-positive "missing" errors during normal provisioning windows.
_BATCH_JOB_QUEUE_UNUSABLE: frozenset[str] = frozenset({"DELETING", "DELETED", "INVALID"})


def _check_batch_job_queue(client: Any, ref: str) -> None:
    resp = client.describe_job_queues(jobQueues=[ref])
    usable = [q for q in resp.get("jobQueues", []) if q.get("status") not in _BATCH_JOB_QUEUE_UNUSABLE]
    if not usable:
        raise _NotFoundError(ref)


def _check_batch_job_def(client: Any, ref: str) -> None:
    resp = client.describe_job_definitions(jobDefinitions=[ref], status="ACTIVE")
    if not resp.get("jobDefinitions"):
        raise _NotFoundError(ref)


def _check_athena_workgroup(client: Any, ref: str) -> None:
    # get_work_group raises InvalidRequestException when the workgroup doesn't exist;
    # no status filter needed. A DISABLED workgroup still exists — the check is
    # existence only, not health. Same rationale as sfn/lambda/glue.
    client.get_work_group(WorkGroup=ref)


_RESOURCE_CHECKERS: Dict[str, Callable[[Any, str], None]] = {
    "sfn": _check_sfn,
    "lambda": _check_lambda,
    "glue": _check_glue_job,
    "ecs_cluster": _check_ecs_cluster,
    "ecs_task_definition": _check_ecs_task_def,
    "batch_job_queue": _check_batch_job_queue,
    "batch_job_definition": _check_batch_job_def,
    "athena_workgroup": _check_athena_workgroup,
}

_KIND_TO_SERVICE: Dict[str, str] = {
    "sfn": "stepfunctions",
    "lambda": "lambda",
    "glue": "glue",
    "ecs_cluster": "ecs",
    "ecs_task_definition": "ecs",
    "batch_job_queue": "batch",
    "batch_job_definition": "batch",
    "athena_workgroup": "athena",
}

_IAM_ACTION: Dict[str, str] = {
    "sfn": "states:DescribeStateMachine",
    "lambda": "lambda:GetFunction",
    "glue": "glue:GetJob",
    "ecs_cluster": "ecs:DescribeClusters",
    "ecs_task_definition": "ecs:DescribeTaskDefinition",
    "batch_job_queue": "batch:DescribeJobQueues",
    "batch_job_definition": "batch:DescribeJobDefinitions",
    "athena_workgroup": "athena:GetWorkGroup",
}

_KIND_DISPLAY: Dict[str, str] = {
    "sfn": "sfn",
    "lambda": "lambda",
    "glue": "glue",
    "ecs_cluster": "ecs",
    "ecs_task_definition": "ecs",
    "batch_job_queue": "batch",
    "batch_job_definition": "batch",
    "athena_workgroup": "athena",
}

_DISPLAY_COL_WIDTH = 9  # pad kind column; longest current display label is 6 chars ("lambda"/"athena")
_CROSS_ACCOUNT_LIMIT = 10

# Kinds where _NotFoundError may mean "deregistered" rather than truly absent.
_LIFECYCLE_KINDS: frozenset[str] = frozenset(
    {
        "ecs_cluster",
        "ecs_task_definition",
        "batch_job_queue",
        "batch_job_definition",
    }
)


# ---- Helpers ----------------------------------------------------------------


def _region_from_arn(arn: str, fallback: str) -> str:
    """Parse AWS region from an ARN; return fallback for non-ARN strings."""
    parts = arn.split(":")
    return parts[3] if len(parts) > 3 and parts[3] else fallback


# ---- Main check function ----------------------------------------------------


def check_resource_references(
    dag: "DAG",
    session: "boto3.Session",
    region: Optional[str],
) -> ResourceCheckResult:
    """Check all external resource references in dag.tasks.

    Returns ResourceCheckResult. Caller is responsible for aborting on
    result.errors (typically sys.exit(1) in deploy.py).
    """
    refs = _collect_resource_refs(dag)
    result = ResourceCheckResult()
    seen: Set[Tuple[str, str]] = set()
    clients: Dict[Tuple[str, str], Any] = {}

    effective_region = region or session.region_name or "us-east-1"
    _region_unset = not (region or session.region_name)
    _region_warned = False

    for rr in refs:
        if rr.role != "same":
            result.skipped.append(rr)
            continue

        if "${" in rr.ref:
            continue  # CloudFormation substitution — resolved at CFN deploy time

        dedup = (rr.kind, rr.ref)
        if dedup in seen:
            continue
        seen.add(dedup)

        if _region_unset and not _region_warned:
            _warn_no_region()
            _region_warned = True
            result.region_warned = True

        ref_region = _region_from_arn(rr.ref, effective_region) if rr.ref.startswith("arn:") else effective_region
        service = _KIND_TO_SERVICE[rr.kind]
        client_key = (service, ref_region)
        if client_key not in clients:
            # boto3-stubs overloads Session.client() with Literal service names; assigning
            # to Any first avoids a spurious call-overload error without a type: ignore
            # (which triggers unused-ignore in CI where boto3-stubs is absent).
            _sess: Any = session
            clients[client_key] = _sess.client(service, region_name=ref_region)
        client = clients[client_key]

        checker = _RESOURCE_CHECKERS[rr.kind]
        result._checked += 1
        try:
            checker(client, rr.ref)
        except _NotFoundError:
            result.errors.append(rr)
            result.failed_refs.add((rr.kind, rr.ref))
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in _CANNOT_VERIFY_CODES:
                result.warnings.append(rr)
                if code in _ACCESS_DENIED_CODES:
                    result.access_denied.append(rr)
                    result.failed_refs.add((rr.kind, rr.ref))
            else:
                result.errors.append(rr)
                result.failed_refs.add((rr.kind, rr.ref))
        except BotoCoreError:
            result.warnings.append(rr)

    return result


# ---- Output formatting -------------------------------------------------------


def print_resource_check_result(result: ResourceCheckResult) -> None:
    """Print the resource check output.

    Caller must check result.errors and call sys.exit(1) if non-empty — this
    function prints the abort message but does not exit. All ref, role, and
    task_id values are passed through _sanitize before output.
    """
    has_anything = bool(result.errors or result.warnings or result.skipped or result._checked)
    if not has_anything:
        return

    if not result.errors and not result.warnings and not result.skipped:
        print(f"Checking referenced AWS resources... {_c('✓', BOLD_GREEN)}")
        return

    print("Checking referenced AWS resources...")

    for rr in result.errors:
        kind = _KIND_DISPLAY.get(rr.kind, rr.kind)
        not_found_msg = "does not exist or is deregistered" if rr.kind in _LIFECYCLE_KINDS else "does not exist"
        print(
            _c(
                f"  ✗ {kind:<{_DISPLAY_COL_WIDTH}} (task: {_sanitize(rr.task_id)})"
                f" — {not_found_msg}: {_sanitize(rr.ref)}",
                RED,
            )
        )

    if result.warnings:
        iam_actions = ", ".join(sorted({_IAM_ACTION[r.kind] for r in result.access_denied if r.kind in _IAM_ACTION}))
        print()
        print(
            _c(
                f"  ⚠  {len(result.warnings)} reference(s) could not be verified:",
                YELLOW,
            )
        )
        if iam_actions:
            print(f"     add {iam_actions} to your deploy role")
        for rr in result.warnings:
            kind = _KIND_DISPLAY.get(rr.kind, rr.kind)
            padded_ref = f"{_sanitize(rr.ref):<20}"
            print(f"       {kind:<{_DISPLAY_COL_WIDTH}} {_c(padded_ref, CYAN)} (task: {_sanitize(rr.task_id)})")

    if result.skipped:
        print()
        print(
            _c(
                f"  ⚠  {len(result.skipped)} reference(s) not verified"
                " — task uses a different IAM role (not assumed during deploy):",
                YELLOW,
            )
        )
        shown = result.skipped[:_CROSS_ACCOUNT_LIMIT]
        for rr in shown:
            kind = _KIND_DISPLAY.get(rr.kind, rr.kind)
            padded_ref = f"{_sanitize(rr.ref):<20}"
            print(
                f"       {kind:<{_DISPLAY_COL_WIDTH}} {_c(padded_ref, CYAN)}"
                f" (task: {_sanitize(rr.task_id)}) → role: {_sanitize(rr.role)}"
            )
        remaining = len(result.skipped) - _CROSS_ACCOUNT_LIMIT
        if remaining > 0:
            print(f"       ... and {remaining} more")

    if result.errors:
        n = len(result.errors)
        noun_verb = "resource does not exist" if n == 1 else "resources do not exist"
        print()
        print(_c(f"❌ {n} {noun_verb} — aborting deploy.", RED))


# ---- Terminal output helpers -------------------------------------------------


def _sanitize(s: Any) -> str:
    """Strip CSI ANSI escape codes and replace newlines, carriage returns, and tabs with spaces.

    Covers CSI sequences only (ESC + '[' + params + letter). OSC sequences
    (ESC + ']', used by hyperlink escape codes) are intentionally out of scope —
    AWS resource names are constrained to alphanumeric/hyphen/underscore and cannot
    contain raw ESC bytes from the API itself.
    """
    cleaned = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', str(s))
    cleaned = cleaned.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return cleaned[:197] + "..." if len(cleaned) > 200 else cleaned


def _warn_no_region() -> None:
    """Print a yellow warning when no AWS region is configured and us-east-1 is used as fallback."""
    print(
        _c(
            "  ⚠  no AWS region configured — checking against us-east-1;"
            " set --region or AWS_DEFAULT_REGION for accurate resource checks",
            YELLOW,
        )
    )


def _numeric_str(v: Any) -> bool:
    """True if v is a finite number or a numeric string — used to compare ECS cpu/memory.

    Accepts digit strings ("512") and float-formatted strings ("512.0") because
    some AWS API responses may return numeric fields as floats.
    Rejects NaN and Inf — `float("nan")` passes `float()` but `int(float("nan"))` raises.
    Rejects booleans explicitly — `isinstance(True, int) is True` in Python, so a
    user-set `container_overrides={"cpu": True}` would otherwise be coerced to `1`
    and produce a confusing `differs: declared=True, actual=1024` line.
    """
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        try:
            int(float(v))
            return True
        except (ValueError, OverflowError):
            return False
    if isinstance(v, str):
        try:
            int(float(v))
            return True
        except (ValueError, OverflowError):
            return False
    return False


# ---- Override result types ---------------------------------------------------


@dataclass
class ParamConflict:
    """A parameter that differs between the declared task config and the AWS resource default."""

    name: str
    declared: Any
    actual: Any


@dataclass
class OverrideReport:
    """Override analysis for one task–resource pair."""

    task_id: str
    service: str  # "glue", "ecs", "batch"
    resource_name: str
    differs: List[ParamConflict] = field(default_factory=list)
    silent_ignores: List[str] = field(default_factory=list)  # Glue NonOverridableArguments
    matching: List[str] = field(default_factory=list)  # declared params that match defaults
    scope_notes: List[str] = field(default_factory=list)  # fields that could not be compared (no baseline available)


@dataclass
class OverrideCheckResult:
    reports: List[OverrideReport] = field(default_factory=list)
    unverified: List[ResourceRef] = field(default_factory=list)
    access_denied_services: Set[str] = field(default_factory=set)  # services that returned IAM denial
    _verified: int = field(default=0, repr=False)  # unique resources fetched successfully (not attempts)
    _skipped: int = field(default=0, repr=False)  # unique resources skipped because in failed_refs
    expired_token: bool = field(default=False)  # True if AWS credentials expired mid-check


# ---- Data fetchers -----------------------------------------------------------


def _fetch_glue_data(client: Any, ref: str) -> Dict[str, Any]:
    job = client.get_job(JobName=ref).get("Job", {})
    if not job:
        raise _NotFoundError(ref)
    return job


def _fetch_ecs_task_def_data(client: Any, ref: str) -> Dict[str, Any]:
    task_def = client.describe_task_definition(taskDefinition=ref).get("taskDefinition", {})
    if not task_def:
        raise _NotFoundError(ref)
    return task_def


def _fetch_batch_job_def_data(client: Any, ref: str) -> Dict[str, Any]:
    resp = client.describe_job_definitions(jobDefinitions=[ref], status="ACTIVE")
    defs = resp.get("jobDefinitions", [])
    if not defs:
        # No ACTIVE definition found — treat as not found so the caller routes
        # this resource to unverified rather than producing a false-clean report.
        raise _NotFoundError(ref)
    return max(defs, key=lambda d: d.get("revision") or 0)


# ---- Per-service override checkers ------------------------------------------


def _check_glue_override(task: Any, job: Dict[str, Any]) -> Optional[OverrideReport]:
    differs: List[ParamConflict] = []
    silent_ignores: List[str] = []
    matching: List[str] = []

    for attr, field_name in (
        ("worker_type", "WorkerType"),
        ("number_of_workers", "NumberOfWorkers"),
        ("max_capacity", "MaxCapacity"),
        ("allocated_capacity", "AllocatedCapacity"),
    ):
        declared = getattr(task, attr, None)
        if declared is None:
            # Glue capacity models are mutually exclusive (worker model vs DPU model).
            # Undeclared fields from the other model are always absent on the task, so
            # `declared is None` already skips them without a separate guard.
            continue
        actual = job.get(field_name)
        if actual is None:
            continue
        if declared != actual:
            differs.append(ParamConflict(field_name, declared, actual))
        else:
            matching.append(f"{field_name}={declared}")

    non_overridable = set(job.get("NonOverridableArguments") or {})
    default_args: Dict[str, str] = job.get("DefaultArguments") or {}
    for arg_name, arg_val in (task.glue_arguments or {}).items():
        if arg_name in non_overridable:
            silent_ignores.append(arg_name)
        elif arg_name in default_args:
            if default_args[arg_name] != arg_val:
                differs.append(ParamConflict(arg_name, arg_val, default_args[arg_name]))
            else:
                matching.append(f"{arg_name}={arg_val}")

    if not (differs or silent_ignores or matching):
        return None
    return OverrideReport(
        task_id=task.task_id,
        service="glue",
        resource_name=task.job_name,
        differs=differs,
        silent_ignores=silent_ignores,
        matching=matching,
    )


def _check_ecs_override(task: Any, task_def: Dict[str, Any]) -> Optional[OverrideReport]:
    declared_overrides = task.container_overrides
    if not declared_overrides:
        return None

    differs: List[ParamConflict] = []
    matching: List[str] = []
    scope_notes: List[str] = []

    # Task-level cpu/memory sit at the top level of container_overrides (not inside
    # containerOverrides[*]). Both sides use CPU units (256/512/1024/…) and memory in
    # MiB — same int normalization as container-level size fields.
    for size_key in ("cpu", "memory"):
        if size_key in declared_overrides:
            base_val = task_def.get(size_key)
            if base_val is None:
                # EC2 launch type without task-level resource limits — no baseline.
                scope_notes.append(
                    f"task.{size_key} — EC2 task definition has no task-level {size_key} limit (not compared)"
                )
            elif _numeric_str(declared_overrides[size_key]) and _numeric_str(base_val):
                d_val = (
                    int(float(declared_overrides[size_key]))
                    if isinstance(declared_overrides[size_key], str)
                    else declared_overrides[size_key]
                )
                a_val = int(float(base_val)) if isinstance(base_val, str) else base_val
                if d_val != a_val:
                    differs.append(ParamConflict(f"task.{size_key}", d_val, a_val))
                else:
                    matching.append(f"task.{size_key}={d_val}")
            # If _numeric_str fails on declared or actual, comparison is silently skipped
            # (non-numeric value — no meaningful comparison is possible).

    # Build container lookup; first entry wins on duplicate names.
    containers_by_name: Dict[str, Dict[str, Any]] = {}
    for c in task_def.get("containerDefinitions", []):
        name = c.get("name", "")
        if name and name not in containers_by_name:
            containers_by_name[name] = c

    for co in declared_overrides.get("containerOverrides") or []:
        cname = co.get("name", "")
        if not cname:
            continue  # entry without a container name cannot be matched against task def
        base = containers_by_name.get(cname)
        if base is None:
            # Container not found in task def — advisory check can't compare without
            # a baseline; skip rather than compare against empty defaults.
            continue

        if "command" in co:
            base_cmd = base.get("command", [])
            if co["command"] != base_cmd:
                differs.append(ParamConflict("command", co["command"], base_cmd))
            else:
                matching.append(f"command ({cname})")

        for size_key in ("cpu", "memory"):
            if size_key in co:
                base_val = base.get(size_key)
                if _numeric_str(co[size_key]) and _numeric_str(base_val):
                    # Normalize to int before comparing (ECS cpu/memory are always integers in
                    # the API). The decorator enforces int on these fields (polyris/CLAUDE.md:
                    # "decorator is the sole validation boundary"), so the string branch is a
                    # defensive guard for direct dict construction only.
                    d_val = int(float(co[size_key])) if isinstance(co[size_key], str) else co[size_key]
                    a_val = int(float(base_val)) if isinstance(base_val, str) else base_val
                    if d_val != a_val:
                        differs.append(ParamConflict(size_key, d_val, a_val))
                    else:
                        matching.append(f"{size_key}={d_val} ({cname})")
                # If base_val is None, the container definition has no cpu/memory set — there is
                # no default to compare against, so the entry is skipped (same rationale as
                # container-not-found above: advisory check requires a baseline to be meaningful).

        base_env: Dict[str, str] = {
            e.get("name", ""): e.get("value", "") for e in base.get("environment", []) if e.get("name")
        }
        for env_var in co.get("environment", []):
            env_name = env_var.get("name", "")
            if not env_name:
                continue
            env_val = env_var.get("value", "")
            if env_name in base_env:
                if base_env[env_name] != env_val:
                    differs.append(ParamConflict(f"env:{env_name}", env_val, base_env[env_name]))
                else:
                    matching.append(f"env:{env_name}={env_val}")

    if not (differs or matching or scope_notes):
        return None
    return OverrideReport(
        task_id=task.task_id,
        service="ecs",
        resource_name=task.task_definition,
        differs=differs,
        silent_ignores=[],
        matching=matching,
        scope_notes=scope_notes,
    )


def _check_batch_override(task: Any, job_def: Dict[str, Any]) -> Optional[OverrideReport]:
    if not task.batch_parameters:
        return None

    default_params: Dict[str, str] = job_def.get("parameters") or {}
    differs: List[ParamConflict] = []
    matching: List[str] = []

    for param_name, param_val in task.batch_parameters.items():
        declared_str = str(param_val)
        if param_name in default_params:
            actual_str = str(default_params[param_name])
            if actual_str != declared_str:
                differs.append(ParamConflict(param_name, declared_str, actual_str))
            else:
                matching.append(f"{param_name}={declared_str}")

    if not (differs or matching):
        return None
    return OverrideReport(
        task_id=task.task_id,
        service="batch",
        resource_name=task.job_definition,
        differs=differs,
        silent_ignores=[],
        matching=matching,
    )


class _OverrideEntry(NamedTuple):
    """Config for one override-checkable task type."""

    kind: str  # key for failed_refs; must match the kind string in _TASK_TYPE_TO_KINDS and _RESOURCE_CHECKERS
    service: str  # boto3 service name (e.g. "glue", "ecs", "batch")
    ref_attr: str  # attribute name on the Task object (e.g. "job_name")
    # NOTE: fetcher may raise _NotFoundError (e.g. _fetch_batch_job_def_data when
    # no ACTIVE revision exists). check_resource_overrides catches it.
    fetcher: Callable[[Any, str], Dict[str, Any]]
    checker: Callable[[Any, Dict[str, Any]], Optional[OverrideReport]]


# Registry of task types that have checkable override params.
# When adding a new task type WITH override params:
#   (1) add its _OverrideEntry here
#   (2) write a data fetcher (_fetch_<service>_data(client, ref) -> Dict[str, Any])
#   (2b) write an override checker (_check_<service>_override(task, data) -> Optional[OverrideReport])
#   (3) add entry to _OVERRIDE_IAM_ACTION (service → IAM action string)
#   (4) test: fetcher, checker, orchestrator dedup, failed_refs skip
#   (_OVERRIDE_CHECKERS entries for types in this dict are auto-derived — no manual step needed there)
# When adding a new task type WITHOUT override params (e.g. a new SFN variant):
#   (1) add "new_type": None to _OVERRIDE_CHECKERS below (this step is NOT auto-derived)
_OVERRIDE_INFO: Dict[str, _OverrideEntry] = {
    "glue": _OverrideEntry("glue", "glue", "job_name", _fetch_glue_data, _check_glue_override),
    "ecs": _OverrideEntry(
        "ecs_task_definition", "ecs", "task_definition", _fetch_ecs_task_def_data, _check_ecs_override
    ),
    "batch": _OverrideEntry(
        "batch_job_definition", "batch", "job_definition", _fetch_batch_job_def_data, _check_batch_override
    ),
}

# Completeness gate: every TaskTypeLiteral must appear here — None means the
# task type carries no override params that can be compared pre-deploy.
# Checkers for types in _OVERRIDE_INFO are auto-derived from info.checker — do not
# edit _OVERRIDE_CHECKERS directly for those types; edit _OVERRIDE_INFO instead.
_OVERRIDE_CHECKERS: Dict[str, Optional[Callable[..., Optional[OverrideReport]]]] = {
    **{t: info.checker for t, info in _OVERRIDE_INFO.items()},
    "sfn": None,  # static ARN reference; no parameterised call to compare
    "lambda": None,  # invocation payload is task-specific, not a resource default
    "athena": None,  # query_string is fully declared in the DAG
    "emr": None,  # emr_step is submitted dynamically per-run; no job-level defaults to compare
}


def check_resource_overrides(
    dag: "DAG",
    session: "boto3.Session",
    region: Optional[str],
    *,
    failed_refs: Optional[Set[Tuple[str, str]]] = None,
    region_warned: bool = False,
) -> OverrideCheckResult:
    """Check override parameter alignment for Glue, ECS, and Batch tasks.

    Advisory only — never aborts deploy. Detects when declared task parameters
    differ from AWS resource defaults, surfacing silent runtime overrides at
    deploy time.

    ``failed_refs`` should be ``ResourceCheckResult.failed_refs`` from the
    preceding ``check_resource_references`` call so resources that could not be
    verified (errors + access_denied) are skipped here too. Pass ``None`` (the
    default) to run the override check independently without skipping any
    resource — the check will then re-attempt every resource regardless of
    prior existence-check results.

    ``region_warned`` should be ``ResourceCheckResult.region_warned`` from the
    preceding ``check_resource_references`` call so the "no AWS region
    configured" warning is not printed a second time when both checks run.
    """
    effective_region = region or session.region_name or "us-east-1"
    _region_unset = not (region or session.region_name)
    _fr: Set[Tuple[str, str]] = failed_refs if failed_refs is not None else set()
    result = OverrideCheckResult()
    clients: Dict[Tuple[str, str], Any] = {}
    fetched: Dict[Tuple[str, str], Any] = {}  # (kind, ref) → data; deduplicates API calls
    unverified_seen: Set[Tuple[str, str]] = set()
    skipped_seen: Set[Tuple[str, str]] = set()  # deduplicates _skipped count across tasks
    expired_token = False
    _region_warned = region_warned

    for task in dag.tasks:
        task_type = task.task_type
        if task_type not in _OVERRIDE_INFO:
            continue

        if getattr(task, "role", "same") != "same":
            continue

        kind, service, ref_attr, fetcher, checker = _OVERRIDE_INFO[task_type]
        ref: str = getattr(task, ref_attr, "") or ""
        if not ref or "${" in ref:
            continue

        if (kind, ref) in _fr:
            if (kind, ref) not in skipped_seen:
                skipped_seen.add((kind, ref))
                result._skipped += 1
            continue

        if _region_unset and not _region_warned:
            _warn_no_region()
            _region_warned = True

        fetch_key = (kind, ref)

        # expired_token only blocks NEW fetches — resources already in fetched
        # still have cached data and can be checker-run without a network call.
        if expired_token and fetch_key not in fetched:
            if fetch_key not in unverified_seen:
                unverified_seen.add(fetch_key)
                result.unverified.append(ResourceRef(task_id=task.task_id, kind=kind, ref=ref))
            continue

        if fetch_key not in fetched:
            # A previous task already failed to fetch this resource — skip without retrying.
            if fetch_key in unverified_seen:
                continue

            ref_region = _region_from_arn(ref, effective_region) if ref.startswith("arn:") else effective_region
            client_key = (service, ref_region)
            if client_key not in clients:
                _sess: Any = session
                clients[client_key] = _sess.client(service, region_name=ref_region)
            client = clients[client_key]

            try:
                fetched[fetch_key] = fetcher(client, ref)
                result._verified += 1
            except _NotFoundError:
                # Fetcher found no active definition (e.g. Batch job def deleted
                # in the race window between existence and override checks).
                unverified_seen.add(fetch_key)
                result.unverified.append(ResourceRef(task_id=task.task_id, kind=kind, ref=ref))
                continue
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code == "ExpiredTokenException":
                    expired_token = True
                    result.expired_token = True
                elif code in _ACCESS_DENIED_CODES:
                    result.access_denied_services.add(service)
                unverified_seen.add(fetch_key)
                result.unverified.append(ResourceRef(task_id=task.task_id, kind=kind, ref=ref))
                continue
            except BotoCoreError:
                unverified_seen.add(fetch_key)
                result.unverified.append(ResourceRef(task_id=task.task_id, kind=kind, ref=ref))
                continue

        report = checker(task, fetched[fetch_key])
        if report is not None:
            result.reports.append(report)

    return result


# ---- Override output formatting ----------------------------------------------

_OVERRIDE_COL_WIDTH = 5  # "batch" = 5 chars; "glue" and "ecs" are shorter
_PARAM_LABEL_WIDTH = 16  # "non-overridable:" = 16 chars; widest label in the block

# IAM actions required by check_resource_overrides, keyed by boto3 service name.
# Used to produce actionable IAM hints when access-denied is returned.
_OVERRIDE_IAM_ACTION: Dict[str, str] = {
    "glue": "glue:GetJob",
    "ecs": "ecs:DescribeTaskDefinition",
    "batch": "batch:DescribeJobDefinitions",
}


def print_resource_override_result(result: OverrideCheckResult) -> None:
    """Print override check output.

    Advisory only — this function never calls sys.exit(). Prints a leading
    blank line before any output so the override block is visually separated
    from the existence check output on the caller's terminal. All resource
    names, task IDs, and parameter values are passed through _sanitize before
    output.

    Reports with actual findings (differs / silent_ignores / matching) are
    printed under "⚠  resource parameter overrides:". Reports that contain
    only scope_notes (fields that could not be compared — e.g. EC2 task
    definitions with no task-level cpu/memory baseline) are informational —
    they appear in the success block rather than under the warning header.
    """
    findings_reports = [r for r in result.reports if r.differs or r.silent_ignores or r.matching]
    scope_reports = [r for r in result.reports if not (r.differs or r.silent_ignores or r.matching) and r.scope_notes]

    if not findings_reports and not result.unverified:
        if result._verified > 0:
            print()
            n = result._verified
            noun = "resource" if n == 1 else "resources"
            skip_note = (
                f" ({result._skipped} skipped — already reported in existence check above)"
                if result._skipped > 0
                else ""
            )
            scope_suffix = ""
            if scope_reports:
                n_sc = sum(len(r.scope_notes) for r in scope_reports)
                sc_noun = "field" if n_sc == 1 else "fields"
                scope_suffix = f", {n_sc} {sc_noun} not in V1 scope"
            print(f"  {_c('✓', BOLD_GREEN)} {n} override {noun} — no issues{skip_note}{scope_suffix}")
            for report in scope_reports:
                print(
                    f"       {report.service:<{_OVERRIDE_COL_WIDTH}}  "
                    f"{_sanitize(report.resource_name)} (task: {_sanitize(report.task_id)}):"
                )
                for note in report.scope_notes:
                    label = f"{'not checked:':<{_PARAM_LABEL_WIDTH}}"
                    print(f"         {label} {_sanitize(note)}")
        elif result._skipped > 0:
            print()
            n = result._skipped
            noun = "resource" if n == 1 else "resources"
            print(
                _c(
                    f"  ⚠  override check skipped for {n} {noun}"
                    " — resources already failed existence check (see above)",
                    YELLOW,
                )
            )
        return

    print()
    if findings_reports:
        print(_c("  ⚠  resource parameter overrides:", YELLOW))
        for i, report in enumerate(findings_reports):
            if i > 0:
                print()
            print(
                f"       {report.service:<{_OVERRIDE_COL_WIDTH}}  "
                f"{_sanitize(report.resource_name)} (task: {_sanitize(report.task_id)}):"
            )
            for conflict in report.differs:
                label = f"{'differs:':<{_PARAM_LABEL_WIDTH}}"
                print(
                    f"         {label} {_sanitize(conflict.name)}"
                    f" (declared: {_sanitize(str(conflict.declared))},"
                    f" AWS default: {_sanitize(str(conflict.actual))})"
                )
            for item in report.silent_ignores:
                label = f"{'non-overridable:':<{_PARAM_LABEL_WIDTH}}"
                print(
                    f"         {label} {_sanitize(item)}"
                    " — Glue will silently ignore this value; remove from glue_arguments"
                )
            for note in report.scope_notes:
                label = f"{'not checked:':<{_PARAM_LABEL_WIDTH}}"
                print(f"         {label} {_sanitize(note)}")
            if report.matching:
                params = ", ".join(_sanitize(m) for m in report.matching)
                n = len(report.matching)
                noun = "parameter" if n == 1 else "parameters"
                verb = "matches" if n == 1 else "match"
                print(f"         ({n} {noun} {verb} defaults: {params})")

    if scope_reports:
        if findings_reports:
            print()
        for report in scope_reports:
            print(
                f"       {report.service:<{_OVERRIDE_COL_WIDTH}}  "
                f"{_sanitize(report.resource_name)} (task: {_sanitize(report.task_id)}):"
            )
            for note in report.scope_notes:
                label = f"{'not checked:':<{_PARAM_LABEL_WIDTH}}"
                print(f"         {label} {_sanitize(note)}")

    if result.unverified:
        if findings_reports or scope_reports:
            print()
        n = len(result.unverified)
        noun = "resource" if n == 1 else "resources"
        if result.expired_token:
            print(
                _c(
                    f"  ⚠  override check incomplete — AWS credentials expired mid-check;"
                    f" re-authenticate and re-run polyris-deploy to verify {n} {noun}",
                    YELLOW,
                )
            )
        else:
            print(
                _c(
                    f"  ⚠  override check skipped for {n} {noun} — access denied, throttled, or other transient error",
                    YELLOW,
                )
            )
        if result.access_denied_services:
            iam_actions = ", ".join(
                sorted(_OVERRIDE_IAM_ACTION[s] for s in result.access_denied_services if s in _OVERRIDE_IAM_ACTION)
            )
            if iam_actions:
                print(f"     add {iam_actions} to your deploy role")

    if result._skipped > 0:
        n = result._skipped
        noun = "resource" if n == 1 else "resources"
        print(f"  ({n} {noun} skipped — already reported in existence check above)")
