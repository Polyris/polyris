"""Polyris config for the example pipelines.

Auto-discovered by ``polyris.config`` from any ``examples/<name>/`` directory
(it walks up until it finds a ``config.py`` with an ``ENVIRONMENTS`` dict). These
values target the account where the testing-infra stack was deployed, so the
hardcoded ARNs in the example pipelines resolve to real resources.

Change ``namespace`` to whatever you want the example pipeline stacks named, and
set ``profile`` if you use a named AWS CLI profile (otherwise your default
credentials are used).
"""

ENVIRONMENTS = {
    "dev": {
        "namespace": "polyris-ex",
        "stage": "dev",
        "region": "us-east-1",
        "account_id": "000000000000",
        # "profile": "your-aws-profile",   # optional — omit to use default credentials
        # The example pipelines don't use cross-account roles, so this is empty.
        "roles": {},
    },
}

DEFAULT_STAGE = "dev"
