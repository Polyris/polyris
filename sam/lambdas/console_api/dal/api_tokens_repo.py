"""
Data Access Layer: API tokens (Personal Access Tokens).

Per ADR #65 — a dedicated `api-tokens` table (not the shared pipeline-tokens
table), keeping a security credential separate from operational execution data.

Table schema (api-tokens):
    PK: token_id        (public id, e.g. "tok_ab12cd34" — safe to show in UI)
    GSI hash-index:     PK = token_hash  (the hot auth lookup)
    GSI owner-index:    PK = owner_sub   ("list my tokens" for multi-user/Pro)
    TTL: ttl            (epoch; set only when the token has an expiry)

Only the SHA-256 `token_hash` is stored — never the plaintext. See
`auth.generate_pat`.
"""

from typing import Optional

from boto3.dynamodb.conditions import Key
from config import dynamodb, API_TOKENS_TABLE
from utils import query_all, scan_all, now_iso


class ApiTokensRepo:
    """Repository for API token records in the api-tokens table."""

    def __init__(self):
        self._table_name = API_TOKENS_TABLE

    @property
    def table(self):
        """Lazy table reference (new on every access for Lambda reuse safety)."""
        return dynamodb.Table(self._table_name)

    # ── Reads ─────────────────────────────────────────────────────────────

    def get_by_id(self, token_id: str) -> Optional[dict]:
        return self.table.get_item(Key={'token_id': token_id}).get('Item')

    def get_by_hash(self, token_hash: str) -> Optional[dict]:
        """Hot path: resolve a presented token to its record via hash-index."""
        items = query_all(
            self.table,
            IndexName='hash-index',
            KeyConditionExpression=Key('token_hash').eq(token_hash),
        )
        return items[0] if items else None

    def list_by_owner(self, owner_sub: str) -> list:
        items = query_all(
            self.table,
            IndexName='owner-index',
            KeyConditionExpression=Key('owner_sub').eq(owner_sub),
        )
        items.sort(key=lambda x: x.get('created_at', ''), reverse=True)
        return items

    def list_all(self) -> list:
        """All tokens, newest first. Used in open-core single-user display."""
        items = scan_all(self.table)
        items.sort(key=lambda x: x.get('created_at', ''), reverse=True)
        return items

    # ── Writes ────────────────────────────────────────────────────────────

    def put(self, item: dict) -> None:
        item.setdefault('revoked', False)
        item.setdefault('created_at', now_iso())
        self.table.put_item(Item=item)

    def revoke(self, token_id: str) -> None:
        """Idempotent revoke. Raises ConditionalCheckFailed if id is unknown
        (route maps that to 404)."""
        self.table.update_item(
            Key={'token_id': token_id},
            UpdateExpression='SET revoked = :t, revoked_at = :ts',
            ExpressionAttributeValues={':t': True, ':ts': now_iso()},
            ConditionExpression='attribute_exists(token_id)',
        )

    def touch_last_used(self, token_id: str) -> None:
        """Best-effort last-used stamp. Never blocks auth on a telemetry write."""
        from botocore.exceptions import ClientError, BotoCoreError
        try:
            self.table.update_item(
                Key={'token_id': token_id},
                UpdateExpression='SET last_used_at = :ts',
                ExpressionAttributeValues={':ts': now_iso()},
            )
        except (ClientError, BotoCoreError):
            pass

    def delete(self, token_id: str) -> None:
        """Hard delete. Mainly for tests/manual cleanup; expiry uses TTL."""
        self.table.delete_item(Key={'token_id': token_id})


# Singleton instance
api_tokens_repo = ApiTokensRepo()
