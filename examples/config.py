"""Polyris config for the example pipelines.

Auto-discovered by ``polyris.config`` from any ``examples/<name>/`` directory
(it walks up until it finds a ``config.py`` with an ``ENVIRONMENTS`` dict). These
values target the account where the testing-infra stack was deployed, so the
hardcoded ARNs in the example pipelines resolve to real resources.

Change ``stack_name`` / ``namespace`` / ``account_id`` for your account, and
set ``profile`` if you use a named AWS CLI profile (otherwise your default
credentials are used).

# ─────────────────────────────────────────────────────────────────────
#  Example commands (both read this file)
# ─────────────────────────────────────────────────────────────────────
#
#   # Deploy a pipeline to the "dev" env resolved below:
#   cd examples/01_single_task && polyris-deploy
#
#   # Explicit stage (same when DEFAULT_STAGE = "dev"):
#   polyris-deploy --stage dev
#
#   # Override just one field for a one-off (loud warning; config unchanged):
#   polyris-deploy --stage dev --region us-west-2
#
#   # Deploy the console UI to the same environment:
#   cd ui && ./deploy.sh --stage dev
"""

# ─────────────────────────────────────────────────────────────────────
#  The dict KEYS below ("dev") are the stage names. That's what --stage
#  picks. There is no separate "stage" field inside the dict — the key
#  IS the stage.
# ─────────────────────────────────────────────────────────────────────
ENVIRONMENTS = {
    "dev": {
        # ── SAM CloudFormation stack name ────────────────────────────
        # The name of the *infrastructure* stack that `sam deploy` created
        # (from sam/samconfig.toml → stack_name = "...").
        #
        # Read by:
        #   - polyris-deploy  → describe_stacks(<stack_name>) for wrapper ARN,
        #                       role ARN, DynamoDB table names, results bucket.
        #   - ui/deploy.sh    → describe_stacks(<stack_name>) for the S3 bucket,
        #                       CloudFront distribution, Cognito IDs.
        #
        # MUST match `stack_name` in your sam/samconfig.toml — polyris does
        # not read that file (it may live in another repo).
        "stack_name": "polyris-ex-dev",

        # ── Namespace ────────────────────────────────────────────────
        # Prefix used for the *pipeline* CloudFormation stacks that
        # polyris-deploy creates: "{namespace}-{stage}-polyris-{dag_id}"
        # (where {stage} is the KEY above — "dev" here).
        # Does NOT need to be the same as the SAM stack_name above.
        #
        # Read by: polyris-deploy (pipeline stack naming).
        "namespace": "polyris-ex",

        # ── AWS region ───────────────────────────────────────────────
        # The region of the SAM stack above. All AWS SDK calls in
        # polyris-deploy and ui/deploy.sh use this region.
        #
        # Overridable per invocation with `--region us-west-2` (loud
        # warning) or the AWS_REGION env var.
        "region": "us-east-1",

        # ── Account ID (guard) ───────────────────────────────────────
        # Optional but strongly recommended: polyris-deploy runs
        # sts:GetCallerIdentity and refuses to deploy if the resolved
        # credentials point at a DIFFERENT account. Prevents the classic
        # "oops I had prod profile selected" mistake.
        #
        # Read by: polyris-deploy (account guard).
        "account_id": "000000000000",

        # ── AWS profile ──────────────────────────────────────────────
        # Named profile from ~/.aws/credentials. Omit to use the default
        # profile / instance credentials / SSO session.
        #
        # Read by: polyris-deploy, ui/deploy.sh.
        # Overridable per invocation with `--profile <name>` or the
        # AWS_PROFILE env var.
        # "profile": "your-aws-profile",

        # ── Cross-account role ARNs ──────────────────────────────────
        # Runtime role ARNs referenced by pipeline tasks via
        # `@task.sfn(role="etl")`. Not related to deploy — these are
        # what the tasks assume at execution time.
        #
        # Read by: task code (via config.roles["etl"]).
        # The example pipelines don't use cross-account roles, so empty.
        "roles": {},
    },
}

# Which stage KEY from ENVIRONMENTS is picked when --stage is not passed.
# Must be one of the keys above.
# Read by: polyris-deploy (defaults ENVIRONMENTS[DEFAULT_STAGE]).
DEFAULT_STAGE = "dev"
