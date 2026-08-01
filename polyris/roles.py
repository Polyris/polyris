"""
Cross-account role access for polyris pipelines.

This module provides convenient access to role ARNs configured in pyproject.toml.

Configuration in pyproject.toml:
    config.py ENVIRONMENTS[stage]["roles"]
    data_warehouse = "arn:aws:iam::111111111111:role/dw-role"
    analytics = "arn:aws:iam::222222222222:role/analytics-role"
    etl = "arn:aws:iam::333333333333:role/etl-role"
    # ... add any roles you need

Or via environment variables:
    export POLYRIS_ROLE_DATA_WAREHOUSE="arn:aws:iam::111111111111:role/dw-role"
    export POLYRIS_ROLE_ANALYTICS="arn:aws:iam::222222222222:role/analytics-role"

Usage:
    from polyris.roles import roles
    
    @task.sfn(arn="${var.task}", role=roles["data_warehouse"])
    def my_task(): pass
    
    # With default fallback
    role = roles.get("optional_role", default="arn:aws:iam::...")

See docs/reference/CONFIGURATION.md for details.
"""

from .config import config

# Re-export config.roles as 'roles' for convenient access
roles = config.roles
