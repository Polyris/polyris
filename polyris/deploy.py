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
    2. Resolves environment config (config.py or CLI overrides)
    3. Reads infra ARNs/tables from the SAM CloudFormation stack outputs
    4. Generates ASL JSON
    5. Generates CloudFormation template
    6. Deploys via a CloudFormation change set (boto3)
    7. Calls SFN with register_only=true (pipeline registration)
"""

import hashlib
import json
import os
import sys
import importlib.util
import argparse
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Tuple

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    ProfileNotFound,
    WaiterError,
)

from .generators import (
    generate_step_function_json,
    generate_dag_hash,
)
from .dag import DAG
from .cfn_changeset import (
    TEMPLATE_BODY_LIMIT,
    ChangeSetError,
    _watch_stack_events,
    all_stacks,
    create_change_set,
    describe_stack,
    execute_change_set,
    start_event_watcher,
)
from .platform import (
    NAMESPACE_STAGE_MAX_LEN,
    PARAM_NAMESPACE,
    PARAM_STAGE,
    AmbiguousPlatformStack,
    parameters_of,
    select_platform_stack,
)
from .config import config as polyris_config
from ._ansi import c as _c, colorize_parser, BOLD, BOLD_GREEN, CYAN
from .resource_check import (
    check_resource_references,
    print_resource_check_result,
    check_resource_overrides,
    print_resource_override_result,
)


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
                db,
                tbl,
                catalog_id=getattr(asset, "glue_catalog", "") or None,
                region=getattr(asset, "glue_region", "") or region,
            )
        except Exception as e:
            print(f"  ? {asset_name}: could not read Glue ({type(e).__name__})")
            continue

        inferred = infer_granularity_from_partition_keys(keys)
        if inferred is None:
            print(f"  • {asset_name}: declared {declared}, Glue keys {keys or '(none)'} (no inference)")
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
                    {"CloudWatchLogsLogGroup": {"LogGroupArn": {"Fn::GetAtt": ["PipelineLogGroup", "Arn"]}}}
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
                    "Input": json.dumps(
                        {
                            "triggered_by": "schedule",
                            "schedule": schedule_expr,
                        }
                    ),
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
        "Description": (f"Polyris pipeline: {dag.dag_id}. {dag.description or ''} Managed by polyris-deploy.").strip(),
        "Resources": resources,
        "Outputs": outputs,
    }


# =============================================================================
# Registration
# =============================================================================


def _stamp_registered_at(
    session: "boto3.Session",
    registry_table: str,
    dag_id: str,
) -> None:
    """Write registered_at on pipeline-registry — the "last deploy" timestamp.

    This is the authoritative writer for `registered_at`. The pipeline SFN's
    own Register_Pipeline state deliberately does NOT touch this field, so
    every normal run leaves it alone and the UI's "Deployed at X" stays
    anchored to the actual deploy, not the last execution start.

    First-ever deploy: the registry row doesn't exist yet; UpdateItem upserts
    with just pipeline_name + registered_at. The subsequent register_only SFN
    run fills in sfn_arn / tasks / dag / schedule / asset_schedule via its own
    UpdateItem — the two writes merge into the same row.
    """
    ddb = session.client("dynamodb")
    try:
        ddb.update_item(
            TableName=registry_table,
            Key={"pipeline_name": {"S": dag_id}},
            UpdateExpression="SET registered_at = :now",
            ExpressionAttributeValues={
                ":now": {"S": datetime.now(timezone.utc).isoformat()},
            },
        )
    except (ClientError, BotoCoreError) as e:
        print(f"\n❌ Could not record deploy time for '{dag_id}' on '{registry_table}': {e}")
        sys.exit(1)


def _register_pipeline(
    sfn_arn: str,
    dag_id: str,
    region: str,
    profile: Optional[str] = None,
):
    """Trigger pipeline registration via Step Function."""
    session = boto3.Session(profile_name=profile, region_name=region)
    sfn = session.client("stepfunctions")
    suffix = uuid.uuid4().hex[:8]
    raw_name = f"{dag_id}-register-{suffix}"
    execution_name = raw_name[:80]

    print(f"  Registering pipeline {dag_id}...")
    try:
        sfn.start_execution(
            stateMachineArn=sfn_arn,
            name=execution_name,
            input=json.dumps({"register_only": True, "triggered_by": "polyris-deploy"}),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ExecutionAlreadyExists":
            pass  # idempotent — a concurrent deploy registered first; this one is a no-op
        else:
            raise
    print("  ✅ Registration triggered")


# =============================================================================
# Config resolution + pre-deploy summary
# =============================================================================

# Names of the CloudFormation Outputs the SAM stack exports for pipeline
# deploys. See sam/template.yaml. Kept as a constant so the reader here and any
# tests import the same names — a rename in the template that misses this
# constant is caught immediately (Principle #13).
SAM_STACK_OUTPUT_KEYS = (
    "DependencyWrapperArn",
    "OrchestrationRoleArn",
    "PipelineRegistryTable",
    "PipelineTokensTable",
    "AssetSubscriptionsTable",
    "ResultsBucket",
)


def _read_sam_stack(session: "boto3.Session", stack_name: str) -> Tuple[dict, dict]:
    """Return ({OutputKey: OutputValue}, {ParameterKey: ParameterValue}) for the SAM stack.

    Raises ClientError with a wrapped, actionable message when the stack does
    not exist — the user's likely mistake is that ``sam deploy`` was never run
    with this name, or the name in config.py is stale.
    """
    cfn = session.client("cloudformation")
    try:
        stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    except ClientError as e:
        if "does not exist" in str(e):
            print(f"\n❌ SAM stack '{stack_name}' not found in this account/region.")
            print("   Two things to check:")
            print(
                f"     1) Did you run `sam deploy` for this stack? "
                f"(from sam/samconfig.toml: stack_name = \"{stack_name}\")"
            )
            print("     2) Does the stack_name in your config.py match the one in sam/samconfig.toml?")
            sys.exit(1)
        raise
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs") or []}
    params = {p["ParameterKey"]: p["ParameterValue"] for p in stack.get("Parameters") or []}
    return outputs, params


def _warn_conflicts(*, cli: dict, config: dict) -> None:
    """Print a warning per CLI arg that overrides a config.py value.

    The user asked for explicit visibility (not a hard reject): we still deploy
    with the CLI value — an occasional one-off override is legitimate — but the
    override is loud. A silent override is what caused the "wrong region" class
    of mistakes.
    """
    for key, cli_val in cli.items():
        if cli_val is None:
            continue
        cfg_val = config.get(key)
        if cfg_val is None or cli_val == cfg_val:
            continue
        print(f"⚠️  --{key} {cli_val} overrides config.py ({cfg_val}). Continuing.")


def _print_deploy_summary(
    *,
    dag_id: str,
    stage: str,
    stack_name: str,
    namespace: str,
    region: str,
    profile: Optional[str],
    pipeline_stack: str,
) -> None:
    """Print the resolved deploy target before the first AWS write.

    Shown once per deploy so the operator sees, at a glance, exactly what will
    happen. Consistent field ordering (dag → stage → stack → namespace → region
    → profile → pipeline stack) so tests can pin the exact format.
    """
    print("")
    print(_c("── Deploy target ──────────────────────────────────────────────────", BOLD))
    print(f"   dag:             {_c(dag_id, CYAN)}")
    print(f"   stage:           {_c(stage, CYAN)}")
    print(f"   sam stack:       {_c(stack_name, CYAN)}    ← reads CloudFormation outputs from here")
    print(f"   namespace:       {_c(namespace, CYAN)}")
    print(f"   region:          {_c(region, CYAN)}")
    print(f"   profile:         {_c(profile or '(default)', CYAN)}")
    print(f"   pipeline stack:  {_c(pipeline_stack, CYAN)}")
    print(_c("───────────────────────────────────────────────────────────────────", BOLD))


def _explicit_region(cli_region: Optional[str], stage_config) -> Optional[str]:
    """Region named explicitly: --region > POLYRIS_REGION/AWS_REGION > config.py. None if nothing names one.

    When this is None the deploy uses the AWS profile's own region (``aws
    configure``), so a project without config.py deploys where its profile
    points rather than silently to us-east-1. us-east-1 stays the last resort.
    """
    return (cli_region or os.environ.get("POLYRIS_REGION") or os.environ.get("AWS_REGION")
            or stage_config.get("region"))


def _check_name_lengths(dag: DAG, namespace: str, stage: str) -> None:
    """Exit before any AWS write when IAM role names would exceed 64 characters."""
    _ns_stage_total = len(namespace) + len(stage)
    if _ns_stage_total > NAMESPACE_STAGE_MAX_LEN:
        print(
            f"\n❌ Namespace+Stage too long — cannot deploy '{dag.dag_id}':\n"
            f"   namespace={namespace!r} ({len(namespace)} chars) + "
            f"stage={stage!r} ({len(stage)} chars) = {_ns_stage_total} chars (limit {NAMESPACE_STAGE_MAX_LEN}).\n"
            f"   IAM role names would exceed 64 chars with the polyris suffix. "
            f"Shorten namespace or stage before deploying."
        )
        sys.exit(1)

    # Scheduler role only exists for time-triggered DAGs — guard only fires then.
    # {namespace}-{stage}-polyris-{dag_id}-scheduler-role = ns+1+stage+9+dag_id+15
    if dag.schedule and not dag.is_asset_triggered:
        _scheduler_role_len = len(namespace) + 1 + len(stage) + 9 + len(dag.dag_id) + 15
        if _scheduler_role_len > 64:
            _dag_id_budget = 39 - _ns_stage_total
            print(
                f"\n❌ DAG ID too long for this namespace+stage — cannot deploy '{dag.dag_id}':\n"
                f"   '{namespace}-{stage}-polyris-{dag.dag_id}-scheduler-role' "
                f"= {_scheduler_role_len} chars (IAM limit 64).\n"
                f"   With namespace={namespace!r} + stage={stage!r} ({_ns_stage_total} chars), "
                f"dag_id must be at most {_dag_id_budget} chars (currently {len(dag.dag_id)}).\n"
                f"   Shorten the dag_id before deploying."
            )
            sys.exit(1)


def _platform_naming(platform: dict, *, namespace: str, stage: str, cli_stage: Optional[str]) -> Tuple[str, str]:
    """Namespace/stage for a project without config.py, taken from the discovered platform stack.

    POLYRIS_NAMESPACE and --stage / POLYRIS_STAGE still win when set.
    """
    params = parameters_of(platform)
    if not os.environ.get("POLYRIS_NAMESPACE") and params.get(PARAM_NAMESPACE):
        namespace = params[PARAM_NAMESPACE]
    if not (cli_stage or os.environ.get("POLYRIS_STAGE")) and params.get(PARAM_STAGE):
        stage = params[PARAM_STAGE]
    return namespace, stage


def _discover_platform_stack(session: "boto3.Session", region: str) -> Tuple[Optional[str], Optional[dict]]:
    """Find the platform stack by the tag polyris-up puts on it (ADR #131).

    Returns (name, describe_stacks entry), or (None, None).
    Used only when neither --stack nor config.py names one. Never guesses:
    several platform stacks in the region is an error that asks for --stack.
    Stacks deployed with ``sam deploy`` carry no tag and are not found here;
    they keep working through config.py's stack_name.
    """
    stacks: list = []
    try:
        stacks = all_stacks(session.client("cloudformation"))
        name = select_platform_stack(stacks, include_review=False)  # no outputs yet
    except AmbiguousPlatformStack as e:
        print(f"\n❌ Several Polyris platform stacks in {region}: {', '.join(e.names)}.")
        print("   Pick one with --stack <name>, or set stack_name in config.py.")
        sys.exit(1)
    except (ClientError, BotoCoreError) as e:
        print(f"\n❌ Could not list CloudFormation stacks to find the platform: {e}")
        sys.exit(1)
    if not name:
        return None, None
    print(f"Found platform stack {_c(name, CYAN)} (installed by polyris-up).")
    return name, next(s for s in stacks if s.get("StackName") == name)


def _fail_no_stack_name(stage: str) -> None:
    """Exit with a clear message when stack_name cannot be resolved.

    The user has stage=X but nothing tells polyris which SAM CloudFormation
    stack to read outputs from. The error names the exact config.py field to
    add and points at samconfig.toml (the value must match). Also handles
    the case where config.py doesn't exist yet — the "add stack_name" hint
    is useless there, so we recommend polyris-init instead.
    """
    from polyris.config import _find_project_config

    has_config = _find_project_config() is not None

    print(f"\n❌ No SAM stack_name resolved for stage '{stage}'.")
    print("   No platform stack installed by polyris-up was found in this region either.")
    print(f"   New install? Run {_c('polyris-up', CYAN)}. Wrong region? Pass --region.")
    print("")
    if has_config:
        print("   Add stack_name to your config.py:")
        print("")
        print("       ENVIRONMENTS = {")
        print(f"           \"{stage}\": {{")
        print("               \"stack_name\": \"<name-from-samconfig.toml>\",")
        print("               ...")
        print("           }")
        print("       }")
        print("")
        print("   Or pass --stack <name> on the CLI (matches sam/samconfig.toml).")
    else:
        print("   No config.py was found walking up from the current directory.")
        print("   Create one with:")
        print("")
        print("       polyris-init --project")
        print("")
        print("   then edit ENVIRONMENTS to add the stack_name from your")
        print("   sam/samconfig.toml. Alternatively pass --stack <name> to")
        print("   polyris-deploy directly.")
    sys.exit(1)


# =============================================================================
# Main deploy function
# =============================================================================


def deploy_pipeline(
    dag: DAG,
    *,
    stage: Optional[str] = None,
    region: Optional[str] = None,
    dry_run: bool = False,
    destroy: bool = False,
    log_level: str = "ERROR",
    log_retention_days: int = 30,
    profile: Optional[str] = None,
    stack: Optional[str] = None,
):
    """Deploy a single DAG via CloudFormation.

    All arguments after ``dag`` are keyword-only.
    """

    # Validate the DAG before touching AWS — fail fast on structural errors, the
    # same check polyris-validate runs. Deploying an invalid DAG only fails later
    # (or ships a broken pipeline), so gate here. Skipped on destroy: we only
    # need dag_id to construct the stack name, not a valid pipeline definition.
    if not destroy:
        from polyris.validation import validate_asl_from_dag

        is_valid, validation_errors, _warnings = validate_asl_from_dag(dag, verbose=False)
        if not is_valid:
            print(f"\n❌ Validation failed for '{dag.dag_id}' — not deploying:")
            for err_msg in validation_errors:
                print(f"   - {err_msg}")
            print("   Fix the errors above (or run polyris-validate) and retry.")
            sys.exit(1)

    # ── Resolve config ────────────────────────────────────────────────────
    # Priority per field: CLI arg > env var > config.py > default.
    # Pipeline naming (namespace / stage) is INDEPENDENT of the SAM stack's
    # own Namespace / Stage — config.py owns pipeline naming, the SAM stack
    # only provides ARNs / table names / bucket via CFN outputs.
    cli_region = region
    cli_profile = profile
    cli_stack = stack

    cli_stage = stage
    stage = stage or polyris_config.stage
    stage_config = polyris_config.for_stage(stage)

    profile = profile or stage_config.profile
    # None when nothing names a region; the AWS session below then falls back
    # to the profile's own region (see _explicit_region).
    explicit_region = _explicit_region(region, stage_config)
    namespace = stage_config.namespace

    stack_name_from_config = stage_config.stack_name
    # May still be None here: without --stack or config.py stack_name, the
    # platform stack is discovered by tag once the AWS session exists (ADR #131).
    sam_stack_name = cli_stack or stack_name_from_config

    # Without config.py, a pipeline on a discovered platform takes its
    # namespace/stage from that platform (below), so two platforms in one
    # account can't produce colliding pipeline stack names.
    from polyris.config import _find_project_config

    # Whether the stack came from discovery or --stack, a project without
    # config.py has no naming of its own: it takes the platform's.
    adopt_platform_naming = _find_project_config() is None

    # IAM name guards — skipped on destroy (stack deletion needs only dag_id for
    # the stack name; no IAM roles are created, so length checks are irrelevant).
    if not destroy and not adopt_platform_naming:
        _check_name_lengths(dag, namespace, stage)

    pipeline_stack = f"{namespace}-{stage}-polyris-{dag.dag_id}"

    _warn_conflicts(
        cli={
            "region": cli_region,
            "profile": cli_profile,
            "stack": cli_stack,
        },
        config={
            "region": stage_config.get("region"),
            "profile": stage_config.profile,
            "stack": stack_name_from_config,
        },
    )

    # ── AWS session + credentials check ──────────────────────────────────
    try:
        session = boto3.Session(profile_name=profile, region_name=explicit_region)
        region = session.region_name or "us-east-1"
        if not session.region_name:
            session = boto3.Session(profile_name=profile, region_name=region)
        caller_identity = session.client("sts").get_caller_identity()
    except ProfileNotFound as e:
        print(f"❌ AWS profile not found: {e}")
        print("   Check ~/.aws/config for available profiles.")
        sys.exit(1)
    except NoCredentialsError as e:
        print(f"❌ No AWS credentials found: {e}")
        if not profile:
            print("   Tip: try polyris-deploy --profile <your-profile>")
        sys.exit(1)
    except ClientError as e:
        print(f"❌ AWS credentials error: {e}")
        sys.exit(1)
    except BotoCoreError as e:
        print(f"❌ AWS error: {e}")
        sys.exit(1)

    platform = None
    if not sam_stack_name:
        sam_stack_name, platform = _discover_platform_stack(session, region)
        if not sam_stack_name:
            _fail_no_stack_name(stage)
    elif adopt_platform_naming:
        try:
            platform = describe_stack(session.client("cloudformation"), sam_stack_name)
        except (ClientError, BotoCoreError) as e:
            print(f"\n❌ Could not read platform stack {sam_stack_name}: {e}")
            sys.exit(1)
    if adopt_platform_naming and platform:
        platform_stage = parameters_of(platform).get(PARAM_STAGE)
        if cli_stage and platform_stage and cli_stage != platform_stage:
            print(f"\n❌ --stage {cli_stage}, but platform {sam_stack_name} is stage {platform_stage}.")
            print("   Install that stage first (polyris-up --stage ...), or name its platform with --stack.")
            sys.exit(1)
        namespace, stage = _platform_naming(platform, namespace=namespace, stage=stage, cli_stage=cli_stage)
        pipeline_stack = f"{namespace}-{stage}-polyris-{dag.dag_id}"
    assert sam_stack_name  # narrow Optional for mypy after the exit above
    if not destroy and adopt_platform_naming:
        _check_name_lengths(dag, namespace, stage)

    _print_deploy_summary(
        dag_id=dag.dag_id,
        stage=stage,
        stack_name=sam_stack_name,
        namespace=namespace,
        region=region,
        profile=profile,
        pipeline_stack=pipeline_stack,
    )

    # Guard: verify we're deploying to the expected account
    expected_account = stage_config.get("account_id")
    if expected_account:
        actual_account = caller_identity["Account"]
        if actual_account != expected_account:
            print(f"❌ Account mismatch for stage '{stage}':")
            print(f"   Expected: {expected_account} (from config.py)")
            print(f"   Actual:   {actual_account} (from credentials)")
            print("   Check your --profile or AWS credentials.")
            sys.exit(1)

    # ── Read infra config from the SAM stack outputs ─────────────────────
    # Only ARNs, table names, and bucket are pulled from SAM — those must
    # match the real infra to work. The SAM stack's own Namespace / Stage
    # are cosmetic (they name SAM's own resources); pipeline naming lives
    # in config.py.
    print(f"\nReading SAM stack outputs from {_c(sam_stack_name, CYAN)}...")
    outputs, _params = _read_sam_stack(session, sam_stack_name)

    wrapper_arn = outputs.get("DependencyWrapperArn")
    role_arn = outputs.get("OrchestrationRoleArn")
    registry_table = outputs.get("PipelineRegistryTable")
    tokens_table = outputs.get("PipelineTokensTable")
    asset_subscriptions_table = outputs.get("AssetSubscriptionsTable")
    results_bucket = outputs.get("ResultsBucket")
    console_url = outputs.get("ConsoleUiUrl")

    if not wrapper_arn or not role_arn:
        print(f"❌ SAM stack '{sam_stack_name}' is missing required outputs.")
        missing = [k for k in ("DependencyWrapperArn", "OrchestrationRoleArn") if not outputs.get(k)]
        print(f"   Missing outputs: {', '.join(missing)}")
        print("   The stack may be from an older polyris/SAM version. Redeploy sam/ from the current repo.")
        sys.exit(1)

    stack_name = pipeline_stack

    # Destroy
    if destroy:
        print(f"\nDestroying stack: {_c(stack_name, CYAN)}")
        if not dry_run:
            cfn = session.client("cloudformation")
            seen_ids: set = set()
            try:
                existing = cfn.describe_stack_events(StackName=stack_name)["StackEvents"]
                seen_ids.update(e["EventId"] for e in existing)
            except Exception:
                pass
            try:
                cfn.delete_stack(StackName=stack_name)
            except ClientError as e:
                print(f"\n❌ CloudFormation delete failed: {e}")
                sys.exit(1)
            stop_watching = threading.Event()
            watcher_thread = threading.Thread(
                target=_watch_stack_events,
                args=(cfn, stack_name, seen_ids, stop_watching),
                daemon=True,
            )
            watcher_thread.start()
            try:
                cfn.get_waiter("stack_delete_complete").wait(
                    StackName=stack_name,
                    WaiterConfig={"Delay": 5, "MaxAttempts": 120},
                )
            except WaiterError as e:
                print(f"\n❌ CloudFormation delete failed: {e}")
                sys.exit(1)
            finally:
                stop_watching.set()
                watcher_thread.join(timeout=5)
            print(_c(f"✅ Stack deleted: {stack_name}", BOLD_GREEN))
        else:
            print(f"[dry-run] Would delete stack: {_c(stack_name, CYAN)}")
        return

    # Check that referenced AWS resources exist before committing to a deploy.
    # Runs in both normal and --dry-run paths (consistent with STS + CFN calls
    # already made above). Skipped for --destroy (no resources to verify).
    try:
        _rc = check_resource_references(dag, session, region)
    except ValueError as e:
        print(f"\n❌ Resource check configuration error: {e}")
        sys.exit(1)
    print_resource_check_result(_rc)
    if _rc.errors:
        sys.exit(1)
    try:
        _ro = check_resource_overrides(
            dag, session, region, failed_refs=_rc.failed_refs, region_warned=_rc.region_warned
        )
    except Exception as e:  # noqa: BLE001
        # Advisory-only: a code bug or unexpected AWS error in the override check
        # must never abort the deploy. Broad except is intentional — the check is
        # non-critical and all its AWS calls are already narrowed internally.
        print(f"\n  ⚠  Override check failed unexpectedly ({type(e).__name__}): {e}")
        traceback.print_exc()
    else:
        print_resource_override_result(_ro)

    # Generate template
    print(f"\nGenerating CloudFormation template for: {_c(dag.dag_id, CYAN)}")
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
    print(f"  Template: {_c(str(template_path), CYAN)}")

    if dry_run:
        print(f"\n[dry-run] Would deploy stack: {_c(stack_name, CYAN)}")
        print(f"  Resources: {list(template['Resources'].keys())}")
        print(f"  DAG hash: {_c(generate_dag_hash(dag), CYAN)}")
        return

    # Deploy via a CloudFormation change set — boto3 only, no `aws` CLI
    # (ADR #131). Templates over CloudFormation's inline limit go through the
    # results bucket, as `aws cloudformation deploy --s3-bucket` used to do.
    print(f"\nDeploying stack: {_c(stack_name, CYAN)}")
    cfn = session.client("cloudformation")
    template_body = json.dumps(template)
    source: dict = {"template_body": template_body}
    if len(template_body.encode()) > TEMPLATE_BODY_LIMIT:
        if not results_bucket:
            print(f"\n❌ Template is {len(template_body.encode())} bytes (inline limit {TEMPLATE_BODY_LIMIT}) "
                  "and the platform stack has no ResultsBucket output to upload it to.")
            sys.exit(1)
        key = f"cfn-pipeline-templates/{stack_name}-{hashlib.sha256(template_body.encode()).hexdigest()[:16]}.json"
        session.client("s3").put_object(Bucket=results_bucket, Key=key, Body=template_body.encode())
        source = {"template_url": f"https://{results_bucket}.s3.{region}.amazonaws.com/{key}"}

    stop_narration = start_event_watcher(cfn, stack_name)
    try:
        change_set = create_change_set(cfn, stack_name=stack_name, capabilities=["CAPABILITY_NAMED_IAM"], **source)
        if change_set is None:
            print("  No changes to deploy — the stack already matches this DAG.")
        else:
            execute_change_set(cfn, stack_name=stack_name, change_set=change_set)
    except (ChangeSetError, ClientError, BotoCoreError) as e:
        print(f"\n❌ CloudFormation deploy failed: {e}")
        sys.exit(1)
    finally:
        stop_narration()

    # Explicitly (re-)enforce the schedule's enabled/disabled state.
    #
    # A CloudFormation change set only diffs the new template against what
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
    pipeline_cfn = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in pipeline_cfn.get("Outputs", [])}
    sfn_arn = outputs.get("StateMachineArn")

    # Register pipeline
    if sfn_arn and registry_table:
        # Stamp registered_at first — this is the only writer of that field.
        # The SFN's Register_Pipeline state no longer touches it, so a
        # registration failure (which still writes a _notify_warn_ record)
        # does not need to roll back this timestamp: CFN deploy already
        # succeeded, which is what "Deployed at" claims.
        _stamp_registered_at(session, registry_table, dag.dag_id)
        _register_pipeline(sfn_arn, dag.dag_id, region, profile=profile)

    print(f"\n{_c(f'✅ {dag.dag_id} deployed successfully!', BOLD_GREEN)}")
    if console_url:
        print("\n   Open the Polyris console to view and trigger your pipeline:")
        print(f"   {_c(f'→ {console_url}', CYAN)}")
    print(f"\n   Stack: {stack_name}")


# =============================================================================
# CLI entry point
# =============================================================================


def _load_dag_from_file(path: Path) -> list:
    """Load DAG(s) from a dag.py file.

    Imports pipeline file purely for DAG extraction.
    Pipeline files should not contain AWS calls at module level.
    """
    spec = importlib.util.spec_from_file_location("pipeline", path)
    if (
        spec is None or spec.loader is None
    ):  # pragma: no cover -- defensive: returns a loaded spec for existing .py paths
        raise ImportError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)

    try:
        spec.loader.exec_module(module)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        if code != 0:
            print(f"❌ Error loading pipeline: exited with code {code}")
            sys.exit(1)
        # exit(0) is tolerated — treat as success
    except Exception as e:
        print(f"❌ Error loading pipeline: {e}")
        sys.exit(1)

    # Exclude DAGs that are merely imported from other modules.
    # A DAG is "local" only if it is not referenced by any other currently-loaded module.
    dags_in_other_modules: set = set()
    for mod in list(sys.modules.values()):
        if mod is None or mod is module or not hasattr(mod, '__dict__'):
            continue
        try:
            for val in mod.__dict__.values():
                if isinstance(val, DAG):
                    dags_in_other_modules.add(id(val))
        except RuntimeError:
            pass

    seen: set = set()
    dags = []
    for obj in module.__dict__.values():
        if isinstance(obj, DAG) and id(obj) not in dags_in_other_modules and id(obj) not in seen:
            seen.add(id(obj))
            dags.append(obj)
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
    seen_dag_ids: dict = {}
    for py_file in sorted(dir_path.glob("*.py")):
        try:
            dags = _load_dag_from_file(py_file)
        except SystemExit:
            print(f"  ⚠️  Skipping {py_file} (failed to load)")
            continue
        for dag in dags:
            if dag.dag_id in seen_dag_ids:
                print(
                    f"  ⚠️  Skipping duplicate dag_id '{dag.dag_id}' in {py_file.name} "
                    f"(already defined in {seen_dag_ids[dag.dag_id].name})"
                )
                continue
            seen_dag_ids[dag.dag_id] = py_file
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
    stack: Optional[str] = None,
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
                    stack=stack,
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


def main(force_destroy: bool = False) -> None:
    parser = argparse.ArgumentParser(
        description="Deploy Polyris pipeline via CloudFormation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  polyris-deploy                          # Deploy from current directory (uses DEFAULT_STAGE)
  polyris-deploy --stage prod             # Deploy to prod (config.py picks stack_name, region, profile)
  polyris-deploy --stack polyris-dev      # Override the SAM stack to read outputs from
  polyris-deploy --dry-run                # Preview without deploying
  polyris-deploy --destroy                # Remove pipeline stack
  polyris-deploy --file my_dag.py         # Deploy specific file

  # Bulk (run from the parent directory containing pipeline subdirectories):
  polyris-deploy --all                        # Deploy every subdirectory
  polyris-deploy --only dir1 dir2             # Deploy just these subdirectories
  polyris-deploy --destroy --all              # Destroy every subdirectory
  polyris-deploy --only dir1 --select my-dag  # Bulk + pick one DAG per directory
        """,
    )
    parser.add_argument("--stage", help="Deployment stage (default: from config.py DEFAULT_STAGE)")
    parser.add_argument(
        "--stack",
        help="SAM CloudFormation stack to read outputs from (default: from config.py ENVIRONMENTS[stage].stack_name)",
    )
    parser.add_argument("--region", help="AWS region (default: from config.py ENVIRONMENTS)")
    parser.add_argument("--file", default=None, help="Pipeline file (default: dag.py). Not used with --all/--only.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without deploying")
    parser.add_argument("--destroy", action="store_true", help="Remove pipeline stack")
    parser.add_argument("--log-level", default="ERROR", choices=["ALL", "ERROR", "FATAL", "OFF"])
    parser.add_argument("--log-retention", type=int, default=30, help="Log retention in days")
    parser.add_argument(
        "--select", help="Select specific DAG by ID (for multi-DAG files); combinable with --all/--only"
    )
    parser.add_argument("--profile", help="AWS profile name (from ~/.aws/credentials)")

    bulk_group = parser.add_mutually_exclusive_group()
    bulk_group.add_argument(
        "--all",
        action="store_true",
        help="Bulk mode: deploy/destroy every immediate subdirectory of the current directory",
    )
    bulk_group.add_argument(
        "--only", nargs="+", metavar="DIR", help="Bulk mode: deploy/destroy only the listed subdirectories"
    )

    colorize_parser(parser)
    args = parser.parse_args()
    destroy = args.destroy or force_destroy

    if args.all or args.only:
        if args.file is not None:
            print("❌ --file is not used with --all/--only — each directory's .py files are discovered automatically")
            sys.exit(1)
        target_dirs = args.only if args.only else [str(p) for p in sorted(Path(".").iterdir()) if p.is_dir()]
        _run_bulk(
            target_dirs,
            select=args.select,
            stage=args.stage,
            stack=args.stack,
            region=args.region,
            dry_run=args.dry_run,
            destroy=destroy,
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
    print(f"Loading pipeline: {_c(str(pipeline_file), CYAN)}")
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
            stack=args.stack,
            region=args.region,
            dry_run=args.dry_run,
            destroy=destroy,
            log_level=args.log_level,
            log_retention_days=args.log_retention,
            profile=args.profile,
        )


def main_destroy() -> None:
    """Entry point for `polyris-destroy` — identical to `polyris-deploy --destroy`."""
    main(force_destroy=True)


if __name__ == "__main__":
    main()
