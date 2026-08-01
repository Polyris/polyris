"""
polyris deploy — CloudFormation-based pipeline deployment.

CloudFormation-based pipeline deployment.

Usage:
    # From pipeline directory
    cd pipelines/acme/daily
    polyris-deploy

    # Or with options
    polyris-deploy --stage prod
    polyris-deploy --dry-run
    polyris-deploy --destroy

Workflow:
    1. Reads dag.py (imports DAG)
    2. Reads infra config from SSM (/polyris/{stage}/)
    3. Generates ASL JSON
    4. Generates CloudFormation template
    5. aws cloudformation deploy
    6. Calls SFN with register_only=true (pipeline registration)
"""

import json
import sys
import subprocess
import importlib.util
import argparse
import threading
from pathlib import Path
from typing import Optional, List, Tuple
from datetime import datetime, timezone

import boto3

from .generators import (
    generate_step_function_json,
    generate_dag_hash,
)
from .dag import DAG
from .config import config as polyris_config


# =============================================================================
# CFN Template Generation
# =============================================================================

def _check_glue_granularity_drift(dag: DAG, *, region: Optional[str], profile: Optional[str]) -> None:
    """Advisory check: compare each Glue-backed asset's declared granularity
    to what its Glue PartitionKeys suggest (ADR #50).

    Prints warnings on mismatch but never fails — the declared value always
    wins. Users with naming conventions that don't match the inference
    heuristics (or who know their data better than the heuristics) are
    expected to ignore the warning.
    """
    try:
        from .adapters.glue import (
            fetch_glue_partition_keys,
            infer_granularity_from_partition_keys,
        )
    except ImportError:
        return  # boto3 missing — skip silently

    seen: dict = {}
    for task in getattr(dag, "tasks", []):
        for outlet in getattr(task, "outlets", []) or []:
            glue_table = getattr(outlet, "glue_table", "")
            if not glue_table or "." not in glue_table:
                continue
            seen.setdefault(outlet.name, outlet)

    if not seen:
        return

    print("\nChecking Glue-declared granularity for assets...")
    for asset_name, asset in seen.items():
        glue_table = asset.glue_table
        db, _, tbl = glue_table.partition(".")
        declared = getattr(asset, "granularity", "daily")
        try:
            keys = fetch_glue_partition_keys(
                db, tbl,
                catalog_id=getattr(asset, "glue_catalog", "") or None,
                region=getattr(asset, "glue_region", "") or region,
            )
        except Exception as e:
            print(f"  ? {asset_name}: could not read Glue ({type(e).__name__})")
            continue

        inferred = infer_granularity_from_partition_keys(keys)
        if inferred is None:
            print(f"  • {asset_name}: declared {declared}, Glue keys "
                  f"{keys or '(none)'} (no inference)")
            continue

        if inferred == declared:
            print(f"  ✓ {asset_name}: declared {declared}, Glue confirms")
        else:
            print(
                f"  ⚠ {asset_name}: declared {declared} but Glue partition "
                f"keys {keys} suggest {inferred}. "
                f"If your declaration is correct, ignore this. Otherwise "
                f"update granularity={inferred!r} in your Asset(...)."
            )


def _generate_cfn_template(
    dag: DAG,
    namespace: str,
    stage: str,
    region: str,
    role_arn: str,
    wrapper_arn: str,
    registry_table: str,
    tokens_table: str,
    asset_subscriptions_table: str,
    log_retention_days: int = 30,
    log_level: str = "ERROR",
) -> dict:
    """Generate CloudFormation template for a single pipeline."""

    full_name = f"{namespace}-{stage}-polyris-{dag.dag_id}"
    asl_json = generate_step_function_json(
        dag,
        wrapper_arn=wrapper_arn,
        registry_table=registry_table,
        tokens_table=tokens_table,
        asset_subscriptions_table=asset_subscriptions_table,
    )

    # Fail fast, before anything reaches AWS, if a template variable was never
    # substituted — deploying it anyway just defers the same failure to the
    # first real API call that uses the literal '${...}' string (this is
    # exactly how the asset_subscriptions_table bug surfaced: DynamoDB
    # rejected TableName='${asset_subscriptions_table}' with a
    # ValidationException, well after the stack had already deployed).
    if "${" in asl_json:
        import re
        leftover = sorted(set(re.findall(r"\$\{([a-zA-Z0-9_]+)\}", asl_json)))
        raise ValueError(
            f"Generated Step Functions definition for '{dag.dag_id}' still contains "
            f"unsubstituted template variable(s): {leftover}. This means a required "
            f"parameter was not passed to generate_step_function_json — check "
            f"deploy.py's SSM reads and _generate_cfn_template's call site."
        )

    resources = {}
    outputs = {}

    # Log Group
    resources["PipelineLogGroup"] = {
        "Type": "AWS::Logs::LogGroup",
        "Properties": {
            "LogGroupName": f"/{namespace}/{stage}/polyris/{dag.dag_id}",
            "RetentionInDays": log_retention_days,
            "Tags": [
                {"Key": "Pipeline", "Value": dag.dag_id},
                {"Key": "Stage", "Value": stage},
                {"Key": "Namespace", "Value": namespace},
                {"Key": "ManagedBy", "Value": "polyris-deploy"},
            ],
        },
    }

    # Step Function
    resources["PipelineStateMachine"] = {
        "Type": "AWS::StepFunctions::StateMachine",
        "Properties": {
            "StateMachineName": full_name,
            "RoleArn": role_arn,
            "DefinitionString": asl_json,
            "LoggingConfiguration": {
                "Level": log_level,
                "IncludeExecutionData": False,
                "Destinations": [
                    {
                        "CloudWatchLogsLogGroup": {
                            "LogGroupArn": {
                                "Fn::GetAtt": ["PipelineLogGroup", "Arn"]
                            }
                        }
                    }
                ],
            },
            "Tags": [
                {"Key": "Name", "Value": full_name},
                {"Key": "Pipeline", "Value": dag.dag_id},
                {"Key": "Stage", "Value": stage},
                {"Key": "Namespace", "Value": namespace},
                {"Key": "ManagedBy", "Value": "polyris-deploy"},
            ],
        },
        "DependsOn": "PipelineLogGroup",
    }

    # EventBridge schedule (time-based) — via EventBridge Scheduler
    # (AWS::Scheduler::Schedule), not the classic AWS::Events::Rule, which
    # AWS's own console now labels "legacy" in favor of Scheduler.
    if dag.schedule and not dag.is_asset_triggered:
        schedule_expr = dag._eventbridge_schedule
        scheduler_role_name = f"{full_name}-scheduler-role"

        # Scheduler needs its own execution role — distinct from role_arn
        # (the pipeline's Step Functions execution role) — trusted only by
        # scheduler.amazonaws.com, and only for start_execution on THIS
        # pipeline's own state machine.
        resources["PipelineSchedulerRole"] = {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "RoleName": scheduler_role_name,
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "scheduler.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                },
                "Policies": [
                    {
                        "PolicyName": "InvokePipelineStateMachine",
                        "PolicyDocument": {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Effect": "Allow",
                                    "Action": "states:StartExecution",
                                    "Resource": {"Ref": "PipelineStateMachine"},
                                }
                            ],
                        },
                    }
                ],
                "Tags": [
                    {"Key": "Pipeline", "Value": dag.dag_id},
                    {"Key": "ManagedBy", "Value": "polyris-deploy"},
                ],
            },
        }

        # Logical ID deliberately NOT "PipelineScheduleRule" (the pre-
        # migration name, when this was an AWS::Events::Rule): confirmed via
        # a real deploy attempt that CloudFormation refuses an in-place
        # resource TYPE change on the same logical ID ("Update of resource
        # type is not permitted"). Keeping a distinct name makes this an
        # add (this resource) + remove (the old Events::Rule, now absent
        # from the template) pair instead — which CloudFormation supports.
        # Do not rename this back to "PipelineScheduleRule".
        resources["PipelineSchedule"] = {
            "Type": "AWS::Scheduler::Schedule",
            "Properties": {
                "Name": f"{full_name}-schedule",
                "Description": f"Schedule for {dag.dag_id}",
                "ScheduleExpression": schedule_expr,
                "State": "DISABLED" if dag.is_paused_upon_creation else "ENABLED",
                "FlexibleTimeWindow": {"Mode": "OFF"},
                "Target": {
                    "Arn": {"Ref": "PipelineStateMachine"},
                    "RoleArn": {"Fn::GetAtt": ["PipelineSchedulerRole", "Arn"]},
                    "Input": json.dumps({
                        "triggered_by": "schedule",
                        "schedule": schedule_expr,
                    }),
                },
            },
            "DependsOn": ["PipelineStateMachine", "PipelineSchedulerRole"],
        }

    outputs["StateMachineArn"] = {
        "Description": f"ARN of {dag.dag_id} Step Function",
        "Value": {"Ref": "PipelineStateMachine"},
        "Export": {"Name": f"{namespace}-{stage}-polyris-{dag.dag_id}-arn"},
    }

    outputs["DagHash"] = {
        "Description": "DAG hash for change detection",
        "Value": generate_dag_hash(dag),
    }

    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": (
            f"Polyris pipeline: {dag.dag_id}. "
            f"{dag.description or ''} "
            "Managed by polyris-deploy."
        ).strip(),
        "Resources": resources,
        "Outputs": outputs,
    }


# =============================================================================
# Registration
# =============================================================================

def _register_pipeline(
    sfn_arn: str,
    dag_id: str,
    region: str,
    profile: Optional[str] = None,
):
    """Trigger pipeline registration via Step Function."""
    session = boto3.Session(profile_name=profile, region_name=region)
    sfn = session.client("stepfunctions")
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

    print(f"  Registering pipeline {dag_id}...")
    sfn.start_execution(
        stateMachineArn=sfn_arn,
        name=f"{dag_id}-register-{ts}",
        input=json.dumps({"register_only": True}),
    )
    print("  ✅ Registration triggered")


# ANSI color codes for stack-event status. Only applied when stdout is a
# real terminal — piping `polyris-deploy` output to a file or CI log must
# not embed raw escape codes.
_COLOR_RESET = "\033[0m"
_COLOR_GREEN = "\033[32m"
_COLOR_RED = "\033[31m"
_COLOR_YELLOW = "\033[33m"
_COLOR_CYAN = "\033[36m"


def _colorize_status(status: str) -> str:
    """Color a CloudFormation ResourceStatus by outcome: red for
    failed/rollback, green for complete, cyan for in-progress, yellow for
    anything else (e.g. REVIEW_IN_PROGRESS). Plain text if stdout isn't a
    real terminal (checked at call time, not cached, so tests can patch
    sys.stdout.isatty directly)."""
    if not sys.stdout.isatty():
        return status
    if "FAILED" in status or "ROLLBACK" in status:
        color = _COLOR_RED
    elif "COMPLETE" in status:
        color = _COLOR_GREEN
    elif "IN_PROGRESS" in status:
        color = _COLOR_CYAN
    else:
        color = _COLOR_YELLOW
    return f"{color}{status}{_COLOR_RESET}"


def _watch_stack_events(
    cfn_client,
    stack_name: str,
    seen_event_ids: set,
    stop_event: threading.Event,
    poll_interval: float = 2.0,
) -> None:
    """Print new CloudFormation stack events as they happen, until
    stop_event is set.

    Purely observational — only reads describe_stack_events; never raises,
    so a polling hiccup (or the stack not existing yet, on a fresh CREATE)
    can never affect the actual deploy outcome, which is driven entirely by
    the separate `aws cloudformation deploy` subprocess this runs alongside.

    `seen_event_ids` must already be seeded (by the caller) with any events
    that existed BEFORE this deploy started — CloudFormation keeps full
    event history per stack across past deploys, so without seeding, the
    first poll here would print old, unrelated events from a previous
    deploy as if they were happening right now.
    """
    while not stop_event.is_set():
        try:
            events = cfn_client.describe_stack_events(StackName=stack_name)["StackEvents"]
            new_events = [e for e in events if e["EventId"] not in seen_event_ids]
            new_events.sort(key=lambda e: e["Timestamp"])
            for e in new_events:
                seen_event_ids.add(e["EventId"])
                reason = f" — {e['ResourceStatusReason']}" if e.get("ResourceStatusReason") else ""
                status_colored = _colorize_status(e["ResourceStatus"])
                # Pad the RAW status (not the color-escaped string) so
                # ANSI codes don't throw off the column alignment.
                padding = " " * max(0, 28 - len(e["ResourceStatus"]))
                print(f"    {status_colored}{padding} {e['ResourceType']:<32} {e['LogicalResourceId']}{reason}")

        except Exception as e:
            # Deliberately broad, and deliberately not narrowed to
            # (ClientError, BotoCoreError): this is a daemon *narration* thread
            # wrapping both the AWS call and the event formatting below it. The
            # deploy's correctness does not depend on it, but a traceback from
            # it mid-deploy — or a dead poller — is worse than a skipped tick.
            # ADR #28's narrow rule is for paths whose failure means something;
            # this one's does not. What Principle #11 forbids is the *silent*
            # swallow this used to be (`pass`, no comment), so it now says so.
            print(f"    (progress poll skipped: {e})")
        stop_event.wait(poll_interval)


# =============================================================================
# Main deploy function
# =============================================================================

def deploy_pipeline(
    dag: DAG,
    stage: Optional[str] = None,
    region: Optional[str] = None,
    dry_run: bool = False,
    destroy: bool = False,
    log_level: str = "ERROR",
    log_retention_days: int = 30,
    profile: Optional[str] = None,
):
    """Deploy a single DAG via CloudFormation."""

    # Validate the DAG before touching AWS — fail fast on structural errors, the
    # same check polyris-validate runs. Deploying an invalid DAG only fails later
    # (or ships a broken pipeline), so gate here.
    from polyris.validation import validate_asl_from_dag
    is_valid, validation_errors, _warnings = validate_asl_from_dag(dag, verbose=False)
    if not is_valid:
        print(f"\n❌ Validation failed for '{dag.dag_id}' — not deploying:")
        for err_msg in validation_errors:
            print(f"   - {err_msg}")
        print("   Fix the errors above (or run polyris-validate) and retry.")
        sys.exit(1)

    stage = stage or polyris_config.stage
    region = region or polyris_config.region
    namespace = polyris_config.namespace
    profile = profile or polyris_config.profile

    # Read infra config from SSM
    print(f"\nReading infra config from SSM /polyris/{stage}/...")
    try:
        session = boto3.Session(profile_name=profile, region_name=region)
        # Verify credentials work
        caller_identity = session.client("sts").get_caller_identity()
    except Exception as e:
        err = str(e)
        if "credentials" in err.lower() or "access" in err.lower() or "AuthFailure" in err:
            print(f"❌ AWS credentials error: {err}")
            if not profile:
                print("   Tip: try polyris-deploy --profile <your-profile>")
                print("   Available profiles: check ~/.aws/credentials or ~/.aws/config")
            sys.exit(1)
        session = boto3.Session(region_name=region)
        caller_identity = None

    # Guard: verify we're deploying to the expected account
    stage_config = polyris_config.for_stage(stage)
    expected_account = stage_config.get("account_id")
    if expected_account:
        if caller_identity is None:
            print(f"❌ Could not verify the AWS account for stage '{stage}' "
                  f"(sts:GetCallerIdentity failed for an unexpected reason, "
                  f"not a credentials error). Stage '{stage}' expects account "
                  f"{expected_account}; refusing to deploy without confirming "
                  f"it, to avoid an accidental wrong-account deploy.")
            sys.exit(1)
        actual_account = caller_identity["Account"]
        if actual_account != expected_account:
            print(f"❌ Account mismatch for stage '{stage}':")
            print(f"   Expected: {expected_account} (from config.py)")
            print(f"   Actual:   {actual_account} (from credentials)")
            print("   Check your --profile or AWS credentials.")
            sys.exit(1)
    ssm = session.client("ssm")

    def get_ssm(name: str) -> Optional[str]:
        try:
            return ssm.get_parameter(
                Name=f"/polyris/{stage}/{name}"
            )["Parameter"]["Value"]
        except ssm.exceptions.ParameterNotFound:
            return None

    wrapper_arn = get_ssm("wrapper_arn")
    role_arn = get_ssm("pipeline_execution_role_arn")
    registry_table = get_ssm("pipeline_registry_table")
    tokens_table = get_ssm("pipeline_tokens_table")
    asset_subscriptions_table = get_ssm("asset_subscriptions_table")
    results_bucket = get_ssm("results_bucket")

    if not wrapper_arn or not role_arn:
        print("❌ SSM parameters not found. Run `sam deploy` first.")
        print(f"   Missing: /polyris/{stage}/wrapper_arn or /polyris/{stage}/pipeline_execution_role_arn")
        if not profile:
            print("   Tip: if you have multiple AWS profiles, try: polyris-deploy --profile <your-profile>")
        sys.exit(1)

    stack_name = f"{namespace}-{stage}-polyris-{dag.dag_id}"

    # Destroy
    if destroy:
        print(f"\nDestroying stack: {stack_name}")
        if not dry_run:
            cmd = [
                "aws", "cloudformation", "delete-stack",
                "--stack-name", stack_name,
                "--region", region,
            ]
            if profile:
                cmd += ["--profile", profile]
            subprocess.run(cmd, check=True)
            print(f"✅ Stack deletion initiated: {stack_name}")
        else:
            print(f"[dry-run] Would delete stack: {stack_name}")
        return

    # Generate template
    print(f"\nGenerating CloudFormation template for: {dag.dag_id}")
    template = _generate_cfn_template(
        dag=dag,
        namespace=namespace,
        stage=stage,
        region=region,
        role_arn=role_arn,
        wrapper_arn=wrapper_arn,
        registry_table=registry_table or "",
        tokens_table=tokens_table or "",
        asset_subscriptions_table=asset_subscriptions_table or "",
        log_level=log_level,
        log_retention_days=log_retention_days,
    )

    # Write template to temp file
    template_path = Path(f"/tmp/polyris-{dag.dag_id}-{stage}.json")
    template_path.write_text(json.dumps(template, indent=2))
    print(f"  Template: {template_path}")

    if dry_run:
        print(f"\n[dry-run] Would deploy stack: {stack_name}")
        print(f"  Resources: {list(template['Resources'].keys())}")
        print(f"  DAG hash: {generate_dag_hash(dag)}")
        return

    # Deploy via CloudFormation
    print(f"\nDeploying stack: {stack_name}")
    cmd = [
        "aws", "cloudformation", "deploy",
        "--template-file", str(template_path),
        "--stack-name", stack_name,
        "--region", region,
        "--capabilities", "CAPABILITY_NAMED_IAM",
        "--no-fail-on-empty-changeset",
    ]
    if profile:
        cmd += ["--profile", profile]
    if results_bucket:
        cmd += ["--s3-bucket", results_bucket, "--s3-prefix", "cfn-pipeline-templates"]

    # Background, purely-observational progress display: poll the same
    # stack events the AWS Console shows, printing new ones as `aws
    # cloudformation deploy` (below, unchanged) runs. Seed with whatever
    # events already exist so an UPDATE doesn't print old history from a
    # past deploy as if it's happening now.
    watcher_cfn = session.client("cloudformation")
    seen_event_ids: set = set()
    try:
        existing_events = watcher_cfn.describe_stack_events(StackName=stack_name)["StackEvents"]
        seen_event_ids.update(e["EventId"] for e in existing_events)
    except Exception:
        pass  # fresh stack that doesn't exist yet — nothing to seed
    stop_watching = threading.Event()
    watcher_thread = threading.Thread(
        target=_watch_stack_events,
        args=(watcher_cfn, stack_name, seen_event_ids, stop_watching),
        daemon=True,
    )
    watcher_thread.start()
    try:
        result = subprocess.run(cmd, capture_output=False)
    finally:
        stop_watching.set()
        watcher_thread.join(timeout=5)

    if result.returncode != 0:
        print("\n❌ CloudFormation deploy failed")
        sys.exit(1)

    # Explicitly (re-)enforce the schedule's enabled/disabled state.
    #
    # `aws cloudformation deploy` only diffs the new template against what
    # CloudFormation tracked as the LAST-applied template — it does not
    # re-verify the live resource's actual configuration. So if someone
    # manually disables this schedule via the AWS Console (bypassing CFN), a
    # later `polyris-deploy` with no other template changes reports "No
    # changes to deploy" and leaves the manual change in place — a
    # `polyris-deploy` should bring the environment back to the DAG's
    # declared intent, not just whatever CloudFormation's changeset diff
    # happens to notice. This call runs unconditionally, so it also fixes
    # dag.is_paused_upon_creation having no effect on a fresh deploy (the
    # template's own State property, fixed above, only matters for a
    # from-scratch resource create — CloudFormation doesn't re-apply an
    # unchanged property on an update).
    if dag.schedule and not dag.is_asset_triggered:
        schedule_name = f"{stack_name}-schedule"
        scheduler_client = session.client("scheduler")
        desired_state = "DISABLED" if dag.is_paused_upon_creation else "ENABLED"
        try:
            # EventBridge Scheduler's update_schedule is a full replace, not a
            # toggle — fetch the schedule CFN just deployed and resubmit it
            # unchanged apart from State, rather than re-deriving Target/
            # FlexibleTimeWindow independently here (which would risk a
            # second copy of that construction drifting from the template).
            current = scheduler_client.get_schedule(Name=schedule_name)
            if current.get("State") != desired_state:
                scheduler_client.update_schedule(
                    Name=schedule_name,
                    GroupName=current.get("GroupName", "default"),
                    ScheduleExpression=current["ScheduleExpression"],
                    FlexibleTimeWindow=current["FlexibleTimeWindow"],
                    Target=current["Target"],
                    State=desired_state,
                )
        except Exception as e:
            print(f"⚠️  Could not set schedule state for '{schedule_name}': {e}")

    # Get SFN ARN from stack outputs
    cfn = session.client("cloudformation")
    stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    sfn_arn = outputs.get("StateMachineArn")

    # Register pipeline
    if sfn_arn and registry_table:
        _register_pipeline(sfn_arn, dag.dag_id, region, profile=profile)

    print("\n✅ Pipeline deployed successfully!")
    print(f"   Stack:   {stack_name}")
    print(f"   SFN ARN: {sfn_arn}")


# =============================================================================
# CLI entry point
# =============================================================================

def _load_dag_from_file(path: Path) -> list:
    """Load DAG(s) from a dag.py file.

    Imports pipeline file purely for DAG extraction.
    Pipeline files should not contain AWS calls at module level.
    """
    spec = importlib.util.spec_from_file_location("pipeline", path)
    if spec is None or spec.loader is None:  # pragma: no cover -- defensive: returns a loaded spec for existing .py paths
        raise ImportError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)

    try:
        spec.loader.exec_module(module)
    except SystemExit:
        pass
    except Exception as e:
        print(f"❌ Error loading pipeline: {e}")
        sys.exit(1)

    # Collect all DAGs defined in the file
    dags = [obj for obj in module.__dict__.values() if isinstance(obj, DAG)]
    return dags


def _discover_dags_in_dir(dir_path: Path) -> "List[Tuple[Path, DAG]]":
    """Find every DAG in every .py file directly inside dir_path (non-recursive).

    A .py file with zero DAG objects (a shared config.py, a utils module, etc.)
    contributes nothing and is silently skipped — that's expected, not an error.
    A file that fails to *load* (syntax error, import error, an accidental
    top-level AWS call raising) is reported and skipped too, rather than
    aborting the whole batch — _load_dag_from_file calls sys.exit(1) on load
    failure, which is exactly right for the single-file CLI path but wrong
    here, so it's caught as SystemExit and turned into "skip this file".
    """
    found: "List[Tuple[Path, DAG]]" = []
    for py_file in sorted(dir_path.glob("*.py")):
        try:
            dags = _load_dag_from_file(py_file)
        except SystemExit:
            print(f"  ⚠️  Skipping {py_file} (failed to load)")
            continue
        for dag in dags:
            found.append((py_file, dag))
    return found


def _run_bulk(
    target_dirs: "List[str]",
    *,
    select: Optional[str],
    stage: Optional[str],
    region: Optional[str],
    dry_run: bool,
    destroy: bool,
    log_level: str,
    log_retention_days: int,
    profile: Optional[str],
) -> None:
    """Deploy or destroy every DAG found across target_dirs (--all / --only).

    Each directory is scanned independently (see _discover_dags_in_dir); each
    DAG's deploy_pipeline() call is isolated with its own try/except so one
    failure — a validation error, an AWS error, anything that would normally
    sys.exit(1) in the single-pipeline path — is recorded and the batch moves
    on to the next DAG, rather than aborting everything after it. A summary
    prints at the end regardless of outcome; the process exits non-zero if
    anything failed, so this is safe to use as a CI/script gate.
    """
    action = "destroy" if destroy else "deploy"
    results: "List[Tuple[str, str, bool, Optional[str]]]" = []

    for dir_name in sorted(target_dirs):
        dir_path = Path(dir_name)
        if not dir_path.is_dir():
            print(f"❌ Not a directory: {dir_name}")
            results.append((dir_name, "-", False, "not a directory"))
            continue

        print(f"\n=== {dir_name} ===")
        dags = _discover_dags_in_dir(dir_path)

        if select:
            dags = [(f, d) for f, d in dags if d.dag_id == select]
            if not dags:
                print(f"  (no DAG '{select}' here — skipping)")
                continue

        if not dags:
            print("  (no DAGs found — skipping)")
            continue

        for py_file, dag in dags:
            print(f"  → {action}ing '{dag.dag_id}' (from {py_file.name})")
            try:
                deploy_pipeline(
                    dag=dag,
                    stage=stage,
                    region=region,
                    dry_run=dry_run,
                    destroy=destroy,
                    log_level=log_level,
                    log_retention_days=log_retention_days,
                    profile=profile,
                )
                results.append((dir_name, dag.dag_id, True, None))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
                if code == 0:
                    results.append((dir_name, dag.dag_id, True, None))
                else:
                    results.append((dir_name, dag.dag_id, False, f"exited with code {code}"))
            except Exception as e:  # pragma: no cover -- defensive: deploy_pipeline's own
                                     # error paths all go through sys.exit, this guards
                                     # against a future change that raises instead
                results.append((dir_name, dag.dag_id, False, str(e)))

    succeeded = [r for r in results if r[2]]
    failed = [r for r in results if not r[2]]

    print(f"\n=== Summary: {len(succeeded)} succeeded, {len(failed)} failed ===")
    for dir_name, dag_id, _, error in failed:
        print(f"  ❌ {dir_name}/{dag_id}: {error}")
    for dir_name, dag_id, _, _ in succeeded:
        print(f"  ✅ {dir_name}/{dag_id}")

    sys.exit(1 if failed else 0)


def main():
    parser = argparse.ArgumentParser(
        description="Deploy Polyris pipeline via CloudFormation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  polyris-deploy                    # Deploy from current directory
  polyris-deploy --stage prod       # Deploy to prod
  polyris-deploy --dry-run          # Preview without deploying
  polyris-deploy --destroy          # Remove pipeline stack
  polyris-deploy --file my_dag.py   # Deploy specific file

  # Bulk (run from the parent directory containing pipeline subdirectories):
  polyris-deploy --all                        # Deploy every subdirectory
  polyris-deploy --only dir1 dir2             # Deploy just these subdirectories
  polyris-deploy --destroy --all              # Destroy every subdirectory
  polyris-deploy --only dir1 --select my-dag  # Bulk + pick one DAG per directory
        """,
    )
    parser.add_argument("--stage", help="Deployment stage (default: from config.py DEFAULT_STAGE)")
    parser.add_argument("--region", help="AWS region (default: from config.py ENVIRONMENTS)")
    parser.add_argument("--file", default=None, help="Pipeline file (default: dag.py). Not used with --all/--only.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without deploying")
    parser.add_argument("--destroy", action="store_true", help="Remove pipeline stack")
    parser.add_argument("--log-level", default="ERROR", choices=["ALL", "ERROR", "FATAL", "OFF"])
    parser.add_argument("--log-retention", type=int, default=30, help="Log retention in days")
    parser.add_argument("--select", help="Select specific DAG by ID (for multi-DAG files); combinable with --all/--only")
    parser.add_argument("--profile", help="AWS profile name (from ~/.aws/credentials)")

    bulk_group = parser.add_mutually_exclusive_group()
    bulk_group.add_argument("--all", action="store_true",
                             help="Bulk mode: deploy/destroy every immediate subdirectory of the current directory")
    bulk_group.add_argument("--only", nargs="+", metavar="DIR",
                             help="Bulk mode: deploy/destroy only the listed subdirectories")

    args = parser.parse_args()

    if args.all or args.only:
        if args.file is not None:
            print("❌ --file is not used with --all/--only — each directory's .py files are discovered automatically")
            sys.exit(1)
        target_dirs = args.only if args.only else [
            str(p) for p in sorted(Path(".").iterdir()) if p.is_dir()
        ]
        _run_bulk(
            target_dirs,
            select=args.select,
            stage=args.stage,
            region=args.region,
            dry_run=args.dry_run,
            destroy=args.destroy,
            log_level=args.log_level,
            log_retention_days=args.log_retention,
            profile=args.profile,
        )
        return

    # Find pipeline file
    pipeline_file = Path(args.file or "dag.py")
    if not pipeline_file.exists():
        print(f"❌ Pipeline file not found: {pipeline_file}")
        sys.exit(1)

    # Load DAGs
    print(f"Loading pipeline: {pipeline_file}")
    dags = _load_dag_from_file(pipeline_file)

    if not dags:
        print("❌ No DAG found in pipeline file")
        sys.exit(1)

    # Select DAG
    if args.select:
        dags = [d for d in dags if d.dag_id == args.select]
        if not dags:
            print(f"❌ DAG '{args.select}' not found")
            sys.exit(1)

    if len(dags) > 1 and not args.select:
        print(f"Found {len(dags)} DAGs: {[d.dag_id for d in dags]}")
        print("Deploying all...")

    # Deploy each DAG
    for dag in dags:
        deploy_pipeline(
            dag=dag,
            stage=args.stage,
            region=args.region,
            dry_run=args.dry_run,
            destroy=args.destroy,
            log_level=args.log_level,
            log_retention_days=args.log_retention,
            profile=args.profile,
        )


if __name__ == "__main__":
    main()
