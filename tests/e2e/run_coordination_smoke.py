#!/usr/bin/env python3
"""Run the coordination smoke against a deployed platform in one command.

    make e2e-coordination STACK=<platform stack> [PROFILE=<aws profile>] [REGION=<region>] [ARGS='-k marking']
    python3 tests/e2e/run_coordination_smoke.py --stack <platform stack> [--profile …] [--region …] [pytest args …]

It reads the console API URL, the Cognito pool and the platform's test state
machines from the stack's outputs; deploys the pipelines of
coordination_smoke/dag.py with polyris-deploy, named after the platform; signs
in as a temporary console user, which it deletes again at the end; and runs
test_coordination_smoke.py. The caller's AWS credentials need
cloudformation:DescribeStacks, what polyris-deploy needs
(docs/deployment/IAM_PERMISSIONS.md), and cognito-idp:AdminCreateUser,
AdminSetUserPassword, AdminInitiateAuth and AdminDeleteUser on the pool.
"""
from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import boto3

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
SMOKE_PIPELINES = HERE / "coordination_smoke"
SMOKE_TESTS = HERE / "test_coordination_smoke.py"

#: Stack outputs the run reads (sam/template.yaml; tests/sdk/test_coordination_smoke_runner.py).
REQUIRED_OUTPUTS = ("ConsoleApiUrl", "TestQuickSfnArn")
#: Present only when the platform has Cognito auth; without them the run goes unauthenticated.
COGNITO_OUTPUTS = ("CognitoUserPoolId", "CognitoClientId")
TEST_QUICK_SUFFIX = "-test-quick"


def stack_outputs(session: Any, stack: str) -> Dict[str, str]:
    """The platform stack's outputs; exits when one the run needs is missing."""
    found = session.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in found.get("Outputs", [])}
    missing = [key for key in REQUIRED_OUTPUTS if key not in outputs]
    if missing:
        sys.exit(f"❌ {stack} has no {', '.join(missing)} output: is it a polyris platform stack?")
    return outputs


def sfn_prefix(test_quick_arn: str) -> str:
    """The ARN prefix of the platform's test state machines, which the smoke pipelines run."""
    if not test_quick_arn.endswith(TEST_QUICK_SUFFIX):
        sys.exit(f"❌ TestQuickSfnArn {test_quick_arn!r} does not end with {TEST_QUICK_SUFFIX!r}")
    return test_quick_arn[: -len(TEST_QUICK_SUFFIX)]


def deploy_pipelines(stack: str, profile: Optional[str], region: str, prefix: str) -> None:
    """Deploy (or update) the smoke pipelines on `stack`, named after the platform."""
    # Without POLYRIS_CONFIG the pipelines always take the platform's namespace and stage,
    # so a second run reuses the stacks of the first instead of adding a copy.
    env = {k: v for k, v in os.environ.items() if k != "POLYRIS_CONFIG"}
    env["POLYRIS_SMOKE_SFN_PREFIX"] = prefix
    command = ["polyris-deploy", "--stack", stack, "--region", region]
    if profile:
        command += ["--profile", profile]
    subprocess.run(command, cwd=SMOKE_PIPELINES, env=env, check=True)


@contextmanager
def temporary_sign_in(cognito: Any, pool_id: str, client_id: str) -> Iterator[str]:
    """Yield an ID token of a console user that exists only for this run."""
    username = f"polyris-smoke-{secrets.token_hex(6)}@example.com"
    password = "Smoke-1" + secrets.token_urlsafe(24)  # upper, lower and a digit; longer than the pool's minimum
    cognito.admin_create_user(
        UserPoolId=pool_id, Username=username, MessageAction="SUPPRESS",
        UserAttributes=[{"Name": "email", "Value": username}, {"Name": "email_verified", "Value": "true"}],
    )
    try:
        cognito.admin_set_user_password(UserPoolId=pool_id, Username=username, Password=password, Permanent=True)
        signed_in = cognito.admin_initiate_auth(
            UserPoolId=pool_id, ClientId=client_id, AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
        if "AuthenticationResult" not in signed_in:
            sys.exit(f"❌ Cognito asked for {signed_in.get('ChallengeName')}: the smoke needs a pool without MFA")
        yield signed_in["AuthenticationResult"]["IdToken"]
    finally:
        cognito.admin_delete_user(UserPoolId=pool_id, Username=username)


def run_tests(api_url: str, token: Optional[str], pytest_args: List[str]) -> int:
    env = dict(os.environ, POLYRIS_API_URL=api_url)
    if token:
        env["POLYRIS_ID_TOKEN"] = token
    command = [sys.executable, "-m", "pytest", str(SMOKE_TESTS), "-v", *pytest_args]
    return subprocess.run(command, cwd=REPO_ROOT, env=env).returncode


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the coordination smoke against a deployed platform.")
    parser.add_argument("--stack", required=True, help="The platform stack to test")
    parser.add_argument("--profile", help="AWS profile (default: the standard credential chain)")
    parser.add_argument("--region", help="AWS region (default: the profile's)")
    args, pytest_args = parser.parse_known_args(argv)

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    region = session.region_name
    if not region:
        sys.exit("❌ No AWS region: pass --region (make: REGION=…).")
    outputs = stack_outputs(session, args.stack)

    print(f"→ Deploying the smoke pipelines to {args.stack}")
    deploy_pipelines(args.stack, args.profile, region, sfn_prefix(outputs["TestQuickSfnArn"]))

    if not all(key in outputs for key in COGNITO_OUTPUTS):
        print("→ The platform has no Cognito auth: running without a token")
        return run_tests(outputs["ConsoleApiUrl"], None, pytest_args)
    cognito = session.client("cognito-idp")
    with temporary_sign_in(cognito, outputs["CognitoUserPoolId"], outputs["CognitoClientId"]) as token:
        print("→ Running the smoke as a temporary console user (deleted afterwards)")
        return run_tests(outputs["ConsoleApiUrl"], token, pytest_args)


if __name__ == "__main__":
    sys.exit(main())
