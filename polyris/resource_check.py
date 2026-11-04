"""Pre-deploy check for referenced AWS resources.

Scans dag.tasks for external resource references (SFN ARNs, Lambda names/ARNs,
Glue jobs, ECS clusters/task definitions, Batch queues/job definitions, Athena
workgroups) and verifies each exists before CloudFormation deploy begins.

Skipped silently (no output):
  - CloudFormation substitution variables (${...}) — resolved at CFN deploy time
  - emr tasks — emr_cluster_id is a runtime resource ID, not a static name
  - sagemaker tasks — not yet implemented

Skipped with warning:
  - Tasks using a different IAM role (task.role != "same") — the deploy session
    cannot assume that role; shown so the operator knows what was not checked.

NOTE: dag.steps are not covered in V1 — only dag.tasks are checked.
NOTE: Bulk deploy deduplicates per-DAG; the same resource in 10 DAGs = 10 checks.
      For large bulk deployments against services with low TPS quotas (Glue ~10 TPS,
      Batch ~2 TPS), throttle warnings are expected and non-blocking.
NOTE: Runs on --dry-run as well as normal deploys. Full confidence requires the same
      IAM actions as a normal deploy (states:DescribeStateMachine, lambda:GetFunction,
      glue:GetJob, ecs:DescribeClusters, ecs:DescribeTaskDefinition,
      batch:DescribeJobQueues, batch:DescribeJobDefinitions, athena:GetWorkGroup).
      Access-denied converts to a warning and deploy proceeds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

from botocore.exceptions import BotoCoreError, ClientError

from ._ansi import c as _c, BOLD_GREEN, CYAN, RED, YELLOW

if TYPE_CHECKING:
    import boto3
    from .dag import DAG


# ---- Error codes that mean "cannot verify" (not "resource missing") ----------

_CANNOT_VERIFY_CODES: frozenset[str] = frozenset({
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedAccess",
    "AuthFailure",
    "KmsAccessDeniedException",     # SFN/Lambda with KMS encryption — missing kms:Decrypt
    "KmsThrottlingException",       # KMS throttle during describe — not a missing resource
    "KmsInvalidStateException",     # KMS key disabled/pending deletion — cannot verify
    "ThrottlingException",
    "Throttling",
    "RequestThrottled",
    "ProvisionedThroughputExceededException",
    "RequestLimitExceeded",
    "TooManyRequestsException",
    "ServiceUnavailableException",  # transient service outage — not a missing resource
    "ExpiredTokenException",        # temporary credentials expired mid-check
    "RequestExpired",               # clock-skew SigV4 failure
})

# Subset of _CANNOT_VERIFY_CODES: errors where the fix is "add IAM permission".
# Used to gate the IAM hint in print_resource_check_result — throttle and
# transient errors should never prompt the operator to change their IAM policy.
_ACCESS_DENIED_CODES: frozenset[str] = frozenset({
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedAccess",
    "AuthFailure",
})


# ---- Result types ------------------------------------------------------------

@dataclass
class ResourceRef:
    """A single AWS resource reference extracted from a dag task."""
    task_id: str
    kind: str   # 'sfn','lambda','glue','ecs_cluster','ecs_task_definition',
                # 'batch_job_queue','batch_job_definition','athena_workgroup'
    ref: str    # the ARN or resource name
    role: str = "same"


@dataclass
class ResourceCheckResult:
    errors: List[ResourceRef] = field(default_factory=list)         # confirmed missing → abort
    warnings: List[ResourceRef] = field(default_factory=list)       # could not verify
    access_denied: List[ResourceRef] = field(default_factory=list)  # IAM-denied subset of warnings
    skipped: List[ResourceRef] = field(default_factory=list)        # different IAM role
    _checked: int = field(default=0, repr=False)                    # refs sent to AWS


# ---- Collection --------------------------------------------------------------

# task_types that are skipped entirely — no refs collected, no warnings.
_RESOURCE_SKIP_TYPES: frozenset[str] = frozenset({
    "emr",        # emr_cluster_id is a runtime resource; state can change between check and run
    "sagemaker",  # not yet implemented
})

# Maps each non-skip task_type to the check-kinds it produces.
# When adding a new task type, update ALL SIX of these in the same change:
#   _collect_resource_refs  _TASK_TYPE_TO_KINDS  _RESOURCE_CHECKERS
#   _KIND_TO_SERVICE  _IAM_ACTION  _KIND_DISPLAY
# test_every_task_type_has_checker_or_skip enforces completeness at test time.
_TASK_TYPE_TO_KINDS: Dict[str, List[str]] = {
    "sfn":    ["sfn"],
    "lambda": ["lambda"],
    "glue":   ["glue"],
    "ecs":    ["ecs_cluster", "ecs_task_definition"],
    "athena": ["athena_workgroup"],
    "batch":  ["batch_job_queue", "batch_job_definition"],
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
                refs.append(ResourceRef(task_id=task.task_id, kind="sfn",
                                        ref=task.arn, role=role))

        elif task_type == "lambda":
            ref = task.arn or task.function_name
            if ref:
                refs.append(ResourceRef(task_id=task.task_id, kind="lambda",
                                        ref=ref, role=role))

        elif task_type == "glue":
            if task.job_name:
                refs.append(ResourceRef(task_id=task.task_id, kind="glue",
                                        ref=task.job_name, role=role))

        elif task_type == "ecs":
            if task.cluster:
                refs.append(ResourceRef(task_id=task.task_id, kind="ecs_cluster",
                                        ref=task.cluster, role=role))
            if task.task_definition:
                refs.append(ResourceRef(task_id=task.task_id, kind="ecs_task_definition",
                                        ref=task.task_definition, role=role))

        elif task_type == "athena":
            wg = task.workgroup
            if wg and wg != "primary":
                refs.append(ResourceRef(task_id=task.task_id, kind="athena_workgroup",
                                        ref=wg, role=role))

        elif task_type == "batch":
            if task.job_queue:
                refs.append(ResourceRef(task_id=task.task_id, kind="batch_job_queue",
                                        ref=task.job_queue, role=role))
            if task.job_definition:
                refs.append(ResourceRef(task_id=task.task_id, kind="batch_job_definition",
                                        ref=task.job_definition, role=role))

        elif task_type in _RESOURCE_SKIP_TYPES:
            pass  # emr_cluster_id is runtime; sagemaker not yet implemented

        else:
            raise ValueError(
                f"task_type {task_type!r} is not recognised by the pre-deploy resource check. "
                "This is a polyris internal error — please open an issue."
            )

    # TODO: dag.steps are not covered — steps can reference SFN ARNs and Lambda
    # names (LambdaInvokeStep, StartExecutionStep, etc.). Add step collection
    # once the step-type surface is stable.
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
    active = [c for c in resp.get("clusters", []) if c.get("status") == "ACTIVE"]
    if not active:
        raise _NotFoundError(ref)


def _check_ecs_task_def(client: Any, ref: str) -> None:
    resp = client.describe_task_definition(taskDefinition=ref)
    if resp.get("taskDefinition", {}).get("status") != "ACTIVE":
        raise _NotFoundError(ref)


# Batch job queue lifecycle statuses that mean the queue is gone or broken.
_BATCH_JOB_QUEUE_UNUSABLE: frozenset[str] = frozenset({"DELETING", "DELETED", "INVALID"})


def _check_batch_job_queue(client: Any, ref: str) -> None:
    resp = client.describe_job_queues(jobQueues=[ref])
    usable = [q for q in resp.get("jobQueues", [])
              if q.get("status") not in _BATCH_JOB_QUEUE_UNUSABLE]
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
    "sfn":                 _check_sfn,
    "lambda":              _check_lambda,
    "glue":                _check_glue_job,
    "ecs_cluster":         _check_ecs_cluster,
    "ecs_task_definition": _check_ecs_task_def,
    "batch_job_queue":     _check_batch_job_queue,
    "batch_job_definition":_check_batch_job_def,
    "athena_workgroup":    _check_athena_workgroup,
}

_KIND_TO_SERVICE: Dict[str, str] = {
    "sfn":                 "stepfunctions",
    "lambda":              "lambda",
    "glue":                "glue",
    "ecs_cluster":         "ecs",
    "ecs_task_definition": "ecs",
    "batch_job_queue":     "batch",
    "batch_job_definition":"batch",
    "athena_workgroup":    "athena",
}

_IAM_ACTION: Dict[str, str] = {
    "sfn":                 "states:DescribeStateMachine",
    "lambda":              "lambda:GetFunction",
    "glue":                "glue:GetJob",
    "ecs_cluster":         "ecs:DescribeClusters",
    "ecs_task_definition": "ecs:DescribeTaskDefinition",
    "batch_job_queue":     "batch:DescribeJobQueues",
    "batch_job_definition":"batch:DescribeJobDefinitions",
    "athena_workgroup":    "athena:GetWorkGroup",
}

_KIND_DISPLAY: Dict[str, str] = {
    "sfn":                 "sfn",
    "lambda":              "lambda",
    "glue":                "glue",
    "ecs_cluster":         "ecs",
    "ecs_task_definition": "ecs",
    "batch_job_queue":     "batch",
    "batch_job_definition":"batch",
    "athena_workgroup":    "athena",
}

_DISPLAY_COL_WIDTH = 9   # pad kind column ("sagemaker" = 9 chars, reserved for future)
_CROSS_ACCOUNT_LIMIT = 10

# Kinds where _NotFoundError may mean "deregistered" rather than truly absent.
_LIFECYCLE_KINDS: frozenset[str] = frozenset({
    "ecs_cluster", "ecs_task_definition",
    "batch_job_queue", "batch_job_definition",
})


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
    effective_region = region or session.region_name or "us-east-1"
    refs = _collect_resource_refs(dag)
    result = ResourceCheckResult()
    seen: Set[Tuple[str, str]] = set()
    clients: Dict[Tuple[str, str], Any] = {}

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
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in _CANNOT_VERIFY_CODES:
                result.warnings.append(rr)
                if code in _ACCESS_DENIED_CODES:
                    result.access_denied.append(rr)
            else:
                result.errors.append(rr)
        except BotoCoreError:
            result.warnings.append(rr)

    return result


# ---- Output formatting -------------------------------------------------------

def print_resource_check_result(result: ResourceCheckResult) -> None:
    """Print the resource check output.

    Caller must check result.errors and call sys.exit(1) if non-empty — this
    function prints the abort message but does not exit.
    """
    has_anything = bool(result.errors or result.warnings or result.skipped
                        or result._checked)
    if not has_anything:
        return

    if not result.errors and not result.warnings and not result.skipped:
        print(f"Checking referenced AWS resources... {_c('✓', BOLD_GREEN)}")
        return

    print("Checking referenced AWS resources...")

    for rr in result.errors:
        kind = _KIND_DISPLAY.get(rr.kind, rr.kind)
        not_found_msg = ("does not exist or is deregistered"
                         if rr.kind in _LIFECYCLE_KINDS else "does not exist")
        print(_c(
            f"  ✗ {kind:<{_DISPLAY_COL_WIDTH}} (task: {rr.task_id})"
            f" — {not_found_msg}: {rr.ref}",
            RED,
        ))

    if result.warnings:
        iam_actions = ", ".join(
            sorted({_IAM_ACTION[r.kind] for r in result.access_denied if r.kind in _IAM_ACTION})
        )
        print()
        print(_c(
            f"  ⚠  {len(result.warnings)} reference(s) could not be verified:",
            YELLOW,
        ))
        if iam_actions:
            print(f"     add {iam_actions} to your deploy role")
        for rr in result.warnings:
            kind = _KIND_DISPLAY.get(rr.kind, rr.kind)
            padded_ref = f"{rr.ref:<20}"
            print(f"       {kind:<{_DISPLAY_COL_WIDTH}} {_c(padded_ref, CYAN)} (task: {rr.task_id})")

    if result.skipped:
        print()
        print(_c(
            f"  ⚠  {len(result.skipped)} reference(s) not verified"
            " — task uses a different IAM role (not assumed during deploy):",
            YELLOW,
        ))
        shown = result.skipped[:_CROSS_ACCOUNT_LIMIT]
        for rr in shown:
            kind = _KIND_DISPLAY.get(rr.kind, rr.kind)
            padded_ref = f"{rr.ref:<20}"
            print(f"       {kind:<{_DISPLAY_COL_WIDTH}} {_c(padded_ref, CYAN)}"
                  f" (task: {rr.task_id}) → role: {rr.role}")
        remaining = len(result.skipped) - _CROSS_ACCOUNT_LIMIT
        if remaining > 0:
            print(f"       ... and {remaining} more")

    if result.errors:
        n = len(result.errors)
        noun_verb = "resource does not exist" if n == 1 else "resources do not exist"
        print()
        print(_c(f"❌ {n} {noun_verb} — aborting deploy.", RED))
