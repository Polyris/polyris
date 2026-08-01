"""
Data Access Layer: Pipeline Registry Table.

Encapsulates all DynamoDB operations for the pipeline-registry table
(PIPELINES_TABLE). Used by routes: pipelines, tasks, assets, backfill,
executions, health.

Table schema:
    PK: pipeline_name (str)
"""

from config import dynamodb, PIPELINES_TABLE
from utils import scan_all


class PipelinesRepo:
    """Repository for pipeline-registry table."""

    def __init__(self):
        self._table_name = PIPELINES_TABLE

    @property
    def table(self):
        return dynamodb.Table(self._table_name)

    # ── Single-item operations ────────────────────────────────────────────

    def get(self, pipeline_name: str) -> dict | None:
        """Get a single pipeline by name."""
        response = self.table.get_item(Key={'pipeline_name': pipeline_name})
        return response.get('Item')

    def put(self, item: dict) -> None:
        """Put a pipeline item."""
        self.table.put_item(Item=item)

    def update(self, pipeline_name: str, update_expr: str,
               expr_values: dict = None, expr_names: dict = None) -> dict:
        """Update a pipeline item."""
        params = {
            'Key': {'pipeline_name': pipeline_name},
            'UpdateExpression': update_expr,
        }
        if expr_values:
            params['ExpressionAttributeValues'] = expr_values
        if expr_names:
            params['ExpressionAttributeNames'] = expr_names
        return self.table.update_item(**params)

    # ── Scan operations ───────────────────────────────────────────────────

    def list_all(self, max_items: int = None, **kwargs) -> list:
        """Scan all pipelines with optional pagination."""
        if max_items:
            return scan_all(self.table, max_items=max_items, **kwargs)
        response = self.table.scan(**kwargs)
        return response.get('Items', [])

    def count(self) -> int:
        """Get total count of registered pipelines."""
        response = self.table.scan(Select='COUNT')
        return response.get('Count', 0)

    # ── Alert configuration (ADR #103) ────────────────────────────────────
    # Per-pipeline alert delivery config lives on the pipeline-registry item
    # under the `alert_config` attribute. The DSL declares nothing about alerts;
    # everything is runtime config edited from Settings → Alerts (Team) and read
    # here at failure time.
    #
    # SECRETS vs CONFIG — important: secrets (Slack webhook URL, PagerDuty
    # integration key) must NOT be stored here as plaintext. DynamoDB items are
    # visible to anyone with table read access. Secrets go in **SSM Parameter
    # Store as SecureString** (free on the standard tier, encrypted with the
    # AWS-managed `aws/ssm` KMS key; the project already provisions SSM
    # parameters). This attribute stores only **non-secret config + the SSM
    # parameter name**; the failure path reads the parameter (WithDecryption) at
    # runtime.
    #
    #   alert_config = {
    #       "enabled_channels": ["slack", "pagerduty"],
    #       "slack": {
    #           "channel": "#acme-alerts",          # not secret → here
    #           "mentions": ["@oncall"],            # not secret → here
    #           "webhook_param": "/polyris/alerts/acme-daily/slack-webhook",  # SSM name
    #       },
    #       "pagerduty": {
    #           "severity": "critical",             # not secret → here
    #           "routing_key_param": "/polyris/alerts/acme-daily/pd-key",     # SSM name
    #       },
    #       "email": {"recipients": [...]},         # future channels slot in
    #   }
    #
    # The actual webhook URL / routing key live in SSM (SecureString); the UI
    # writes them there and stores only the parameter name here.

    def get_alert_config(self, pipeline_name: str) -> dict:
        """Return the alert config for a pipeline, or an empty config.

        Never raises on a missing pipeline or missing attribute — alerting must
        degrade safely (no config = no channels = no delivery, not a crash).
        """
        item = self.get(pipeline_name)
        if not item:
            return {"enabled_channels": []}
        cfg = item.get('alert_config')
        if not isinstance(cfg, dict):
            return {"enabled_channels": []}
        cfg.setdefault('enabled_channels', [])
        return cfg

    def set_alert_config(self, pipeline_name: str, alert_config: dict) -> dict:
        """Write the alert config for a pipeline (Settings → Alerts).

        Uses update_item so it touches only `alert_config`, leaving the rest of
        the registry item (sfn_arn, schedule, …) untouched.
        """
        return self.update(
            pipeline_name,
            update_expr='SET alert_config = :cfg',
            expr_values={':cfg': alert_config},
        )

    def migrate_slack_channel(self, pipeline_name: str) -> dict | None:
        """One-shot backfill: seed alert_config from a legacy slack_channel.

        For pipelines registered before ADR #103, the channel lived in the
        baked-in `slack_channel` field. This lifts it into alert_config so the
        new failure path finds it without a redeploy. Idempotent: if
        alert_config already exists, it is left alone. Returns the config
        written, or None if there was nothing to migrate.
        """
        item = self.get(pipeline_name)
        if not item or isinstance(item.get('alert_config'), dict):
            return None
        legacy_channel = item.get('slack_channel')
        if not legacy_channel:
            return None
        cfg = {
            "enabled_channels": ["slack"],
            "slack": {"channel": legacy_channel, "mentions": []},
        }
        self.set_alert_config(pipeline_name, cfg)
        return cfg

    # ── Global settings (single shared record) ────────────────────────────
    #
    # A handful of settings apply to the whole deployment, not one pipeline —
    # e.g. the decision-wait timeout the failure path uses before giving up on a
    # human decision. They live in one reserved registry item so the SFN can read
    # them with the same getItem it already uses for alert_config (ADR #103). The
    # Team UI can edit them; the free tier shows them read-only.

    GLOBAL_SETTINGS_KEY = '__global_settings__'
    DEFAULT_DECISION_TIMEOUT_SECONDS = 18000  # 5h — matches the historical default

    def get_global_settings(self) -> dict:
        """Return the global-settings record, or sensible defaults. Never raises
        on a missing item — the SFN and UI must always get a usable value."""
        item = self.get(self.GLOBAL_SETTINGS_KEY) or {}
        timeout = item.get('decision_timeout_seconds')
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            timeout = self.DEFAULT_DECISION_TIMEOUT_SECONDS
        return {'decision_timeout_seconds': timeout}

    def set_decision_timeout(self, seconds: int) -> dict:
        """Write the global decision-wait timeout (Team-tier action). Uses
        update_item so the single global record is created/updated in place."""
        seconds = int(seconds)
        self.update(
            self.GLOBAL_SETTINGS_KEY,
            'SET decision_timeout_seconds = :t',
            expr_values={':t': seconds},
        )
        return {'decision_timeout_seconds': seconds}


# Singleton instance
pipelines_repo = PipelinesRepo()
