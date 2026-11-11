#!/usr/bin/env python3
"""
Register polyris pipeline manually.

This triggers the pipeline with register_only=true, which:
1. Writes to pipeline_registry
2. Writes to asset_subscriptions (if asset-triggered)
3. Does NOT run the actual tasks

Use cases:
- After deploy, before first asset arrives
- Re-register after manual table cleanup
- Testing registration flow

Note: this command does NOT write `registered_at`. It triggers the pipeline SFN
with register_only=true, which refreshes runtime metadata (tasks, dag, schedule,
etc.) but never touches deploy-owned fields. The Console's "Deployed at" timestamp
is written exclusively by `polyris-deploy`.

Authentication:
- Uses standard AWS credential chain (same as AWS CLI)
- Reads from ~/.aws/credentials and ~/.aws/config
- Supports AWS_PROFILE environment variable
- Supports IAM role assumption for cross-account

Usage:
    polyris-register <sfn-arn>
    polyris-register arn:aws:states:us-east-1:123456789:stateMachine:my-pipeline
    polyris-register --name my-pipeline --profile prod
    polyris-register --name my-pipeline --role-arn arn:aws:iam::123:role/deploy-role
"""

import argparse
import json
import os
import sys
import boto3
from botocore.exceptions import BotoCoreError, ClientError, ProfileNotFound
from typing import Optional

from ._ansi import colorize_parser


def _format_aws_error(e) -> str:
    """Render ClientError / BotoCoreError into a single actionable line.

    For ClientError, extracts the AWS error Code (e.g. AccessDeniedException)
    so operators can grep for it in docs / runbooks. For BotoCoreError, falls
    back to the exception class name — those don't carry an AWS code.
    """
    if isinstance(e, ClientError):
        code = e.response.get('Error', {}).get('Code', 'ClientError')
        msg = e.response.get('Error', {}).get('Message', str(e))
        return f"{code}: {msg}"
    return f"{type(e).__name__}: {e}"


def get_sfn_client(region: str, profile: Optional[str] = None, role_arn: Optional[str] = None):
    """
    Get Step Functions client with optional profile or assumed role.

    Uses standard AWS credential chain (same as AWS CLI):
    - Environment variables (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
    - ~/.aws/credentials
    - ~/.aws/config (for profiles with role_arn, sso, etc.)
    - Instance profile (EC2, ECS, Lambda)

    Args:
        region: AWS region
        profile: AWS profile name (from ~/.aws/credentials or ~/.aws/config)
        role_arn: IAM role ARN to assume

    Returns:
        boto3 Step Functions client

    Raises:
        ProfileNotFound: the named profile is absent from ~/.aws/credentials
            and ~/.aws/config. Propagates so --json callers see a parseable
            failure instead of stdout emptied by a mid-helper sys.exit.
        BotoCoreError: credential chain failure, endpoint resolution error,
            or other botocore-level issue.
        ClientError: AWS API call failure (e.g. assume_role AccessDenied).
    """
    # Use AWS_PROFILE env var if no explicit profile
    if not profile:
        profile = os.environ.get('AWS_PROFILE')
    
    # ProfileNotFound (and any other BotoCoreError) propagates to the caller
    # so main() can route it to --json or text output — exiting here with
    # stderr-only would leave stdout empty and break json.loads for --json
    # callers.
    if role_arn:
        # Assume role
        if profile:
            session = boto3.Session(profile_name=profile, region_name=region)
            sts = session.client('sts')
        else:
            sts = boto3.client('sts', region_name=region)

        response = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName='polyris-register'
        )

        credentials = response['Credentials']
        return boto3.client(
            'stepfunctions',
            region_name=region,
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
    elif profile:
        # Use profile (works with ~/.aws/credentials and ~/.aws/config)
        session = boto3.Session(profile_name=profile, region_name=region)
        return session.client('stepfunctions')
    else:
        # Default credentials chain
        return boto3.client('stepfunctions', region_name=region)


def register_pipeline(sfn_arn: str, region: Optional[str] = None, profile: Optional[str] = None, role_arn: Optional[str] = None) -> dict:
    """
    Register a pipeline by triggering it with register_only=true.
    
    Args:
        sfn_arn: Full ARN of the Step Function
        region: AWS region (optional, extracted from ARN)
        profile: AWS profile name
        role_arn: IAM role ARN to assume
    
    Returns:
        dict with execution details or error
    """
    # Extract region from ARN if not provided
    if not region:
        # ARN format: arn:aws:states:REGION:ACCOUNT:stateMachine:NAME
        parts = sfn_arn.split(':')
        if len(parts) >= 4:
            region = parts[3]
        else:
            region = 'us-east-1'
    
    # Client creation is its own try — if get_sfn_client raises
    # ProfileNotFound (or any BotoCoreError), we can't fall through to the
    # sfn.exceptions.* catches below because `sfn` would be undefined and
    # resolving `sfn.exceptions.StateMachineDoesNotExist` on the except line
    # would NameError past our (ClientError, BotoCoreError) handler.
    try:
        sfn = get_sfn_client(region, profile, role_arn)
    except (ClientError, BotoCoreError) as e:
        return {
            'success': False,
            'error': _format_aws_error(e),
        }

    try:
        # Start execution with deterministic name for idempotency
        import uuid
        sm_name = sfn_arn.split(':')[-1] if ':' in sfn_arn else 'unknown'
        exec_id = uuid.uuid4().hex[:8]
        register_name = f"register-{sm_name[:60]}-{exec_id}"
        response = sfn.start_execution(
            stateMachineArn=sfn_arn,
            name=register_name,
            input=json.dumps({
                'register_only': True,
                'triggered_by': 'polyris-register-cli'
            })
        )
        
        return {
            'success': True,
            'execution_arn': response['executionArn'],
            'started_at': response['startDate'].isoformat()
        }
        
    except sfn.exceptions.StateMachineDoesNotExist:
        return {
            'success': False,
            'error': f"State machine not found: {sfn_arn}"
        }
    except sfn.exceptions.ExecutionAlreadyExists:
        # idempotent — a concurrent registration already started; this is a no-op
        return {
            'success': True,
            'execution_arn': '',
            'started_at': '',
        }
    except (ClientError, BotoCoreError) as e:
        # BotoCoreError covers ProfileNotFound, EndpointConnectionError,
        # credential chain failures — any of those must surface as a failure
        # dict so --json callers get parseable output instead of a traceback.
        return {
            'success': False,
            'error': _format_aws_error(e),
        }


def find_sfn_arn(name: str, region: str, namespace: Optional[str] = None, profile: Optional[str] = None, role_arn: Optional[str] = None) -> Optional[str]:
    """
    Find Step Function ARN by name.

    Args:
        name: Pipeline name (dag_id)
        region: AWS region
        namespace: Optional namespace prefix
        profile: AWS profile name
        role_arn: IAM role ARN to assume

    Returns:
        Full ARN if the paginator finds an exact or namespace-prefixed match;
        None if the paginator succeeds with no match (true "not found").

    Raises:
        ClientError: paginator call failed — AccessDenied, throttling, service
            outage. Callers must distinguish this from None; a bare
            `except ClientError: return None` here turned every IAM denial
            into a misleading "pipeline not found" message.
        ProfileNotFound / BotoCoreError: credential or profile resolution
            failed inside `get_sfn_client`. Propagates for the same reason.
    """
    sfn = get_sfn_client(region, profile, role_arn)

    paginator = sfn.get_paginator('list_state_machines')

    # None means "paginator succeeded, no match" — a genuine "not found".
    # ClientError propagates so the caller can distinguish AccessDenied,
    # throttling, and service outages from "pipeline does not exist"; a
    # bare `except ClientError: return None` here turned every IAM denial
    # into a misleading "pipeline not found" message and sent operators
    # hunting for the wrong problem.
    for page in paginator.paginate():
        for sm in page['stateMachines']:
            sm_name = sm['name']
            # Match exact name or namespace-prefixed name. When a
            # namespace is given, it must actually match (as the prefix
            # segment deploy_pipeline uses: f"{namespace}-{stage}-
            # polyris-{dag_id}") — previously `namespace` was accepted
            # and documented (--namespace CLI help, this docstring) but
            # never actually checked, so two different namespaces
            # sharing a dag_id (e.g. "orders" registered under both
            # "team-a" and "team-b") silently resolved to whichever one
            # the AWS API happened to list first, regardless of which
            # namespace was explicitly requested.
            if namespace and not sm_name.startswith(f"{namespace}-"):
                continue
            if sm_name == name or sm_name.endswith(f'-{name}'):
                return sm['stateMachineArn']

    return None


def main():
    parser = argparse.ArgumentParser(
        description='Register polyris pipeline (writes to DynamoDB without running tasks)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Register by ARN (uses default credentials)
    polyris-register arn:aws:states:us-east-1:123456789:stateMachine:my-pipeline
    
    # Register by name with AWS profile
    polyris-register --name my-pipeline --profile prod
    
    # Use AWS_PROFILE environment variable
    AWS_PROFILE=prod polyris-register --name my-pipeline
    
    # Register by assuming IAM role
    polyris-register --name my-pipeline --role-arn arn:aws:iam::123:role/deploy-role
    
    # Cross-account: use profile to assume role in another account
    polyris-register --name my-pipeline --profile dev --role-arn arn:aws:iam::456:role/prod-deploy

Authentication:
    Uses standard AWS credential chain (same as AWS CLI):
    - Environment variables (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_PROFILE)
    - Shared credentials file (~/.aws/credentials)
    - Config file (~/.aws/config) - supports SSO, role_arn, etc.
    - Instance profile (EC2, ECS, Lambda)
        """
    )
    
    parser.add_argument(
        'arn',
        nargs='?',
        help='Step Function ARN'
    )
    parser.add_argument(
        '--name', '-n',
        help='Pipeline name (alternative to ARN)'
    )
    parser.add_argument(
        '--region', '-r',
        default=None,
        help='AWS region (default: from config.py or us-east-1)'
    )
    parser.add_argument(
        '--namespace',
        help='Namespace prefix for pipeline search'
    )
    parser.add_argument(
        '--profile', '-p',
        help='AWS profile name (from ~/.aws/credentials or ~/.aws/config)'
    )
    parser.add_argument(
        '--role-arn',
        help='IAM role ARN to assume'
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help='Output result as JSON'
    )

    colorize_parser(parser)
    args = parser.parse_args()
    
    # Determine ARN
    sfn_arn = args.arn
    
    # --json: stdout carries exactly one JSON document — no progress text,
    # and failures are reported as {"success": false, "error": ...}.
    if not sfn_arn and args.name:
        if not args.json:
            print(f"🔍 Looking for pipeline '{args.name}' in {args.region}...")
        try:
            sfn_arn = find_sfn_arn(
                args.name,
                args.region,
                args.namespace,
                args.profile,
                args.role_arn,
            )
        except (ClientError, BotoCoreError) as e:
            # Surface the real AWS error instead of "not found" — AccessDenied
            # here means the search never ran, not that the pipeline is absent.
            # BotoCoreError covers ProfileNotFound and credential-chain failures,
            # which otherwise crash with a traceback on stdout.
            error = f"Search failed — {_format_aws_error(e)}"
            # On AccessDenied specifically, point the operator at the IAM
            # action they're missing so they don't chase a non-existent
            # pipeline. Matches the resource_check.py style ("add X to your
            # deploy role").
            if isinstance(e, ClientError) and e.response.get('Error', {}).get('Code', '') == 'AccessDeniedException':
                error += " — ensure your role has states:ListStateMachines"
            if args.json:
                print(json.dumps({'success': False, 'error': error}, indent=2))
            else:
                print(f"❌ {error}")
            sys.exit(1)

        if not sfn_arn:
            error = f"Pipeline '{args.name}' not found in {args.region}"
            if args.json:
                print(json.dumps({'success': False, 'error': error}, indent=2))
            else:
                print(f"❌ {error}")
            sys.exit(1)

        if not args.json:
            print(f"   Found: {sfn_arn}")
    
    if not sfn_arn:
        # `--json` reserves stdout for a single parseable document. Help text
        # there would break `json.loads`; emit the error as JSON and route the
        # usage hint to stderr so interactive and scripted callers both work.
        if args.json:
            print(json.dumps({
                'success': False,
                'error': 'Missing required argument: provide an SFN ARN positional or --name',
            }, indent=2))
            parser.print_help(sys.stderr)
        else:
            parser.print_help()
        sys.exit(1)
    
    if not args.json:
        print("📝 Registering pipeline...")
        if args.profile:
            print(f"   Using profile: {args.profile}")
        elif os.environ.get('AWS_PROFILE'):
            print(f"   Using profile: {os.environ.get('AWS_PROFILE')} (from AWS_PROFILE)")
        if args.role_arn:
            print(f"   Assuming role: {args.role_arn}")
    
    result = register_pipeline(sfn_arn, args.region, args.profile, args.role_arn)
    
    if args.json:
        print(json.dumps(result, indent=2))
        if not result['success']:
            sys.exit(1)
    elif result['success']:
        if result['execution_arn']:
            print("✅ Registration triggered!")
            print(f"   Execution: {result['execution_arn']}")
            print(f"   Started: {result['started_at']}")
            print()
            print("   Pipeline will be registered in:")
            print("   • pipeline_registry (for UI discovery)")
            print("   • asset_subscriptions (for asset triggers)")
        else:
            print("✅ Already registered (concurrent registration in progress — no-op)")
    else:
        print(f"❌ Registration failed: {result['error']}")
        sys.exit(1)


if __name__ == '__main__':  # pragma: no cover
    main()
