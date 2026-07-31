"""
Data Access Layer: Circuit Breakers Table.

Encapsulates DynamoDB operations for the circuit-breaker state table
(CIRCUIT_BREAKER_TABLE).

This table is *optional* — only present when downstream-protection
circuit breakers are wired into the deployment. When the env var is
empty, the singleton's `enabled` flag is False and queries short-circuit
to empty results so callers can stay agnostic.

Table schema (when present):
    PK: service_name
    Attributes: state ("OPEN" / "CLOSED" / "HALF_OPEN"),
                last_failure_time, ...
"""

from boto3.dynamodb.conditions import Attr
from config import dynamodb, CIRCUIT_BREAKER_TABLE


class CircuitBreakersRepo:
    """Repository for circuit-breaker state.

    Optional feature — when CIRCUIT_BREAKER_TABLE env var is empty,
    `enabled` is False and `query_open()` returns []. Callers can therefore
    treat the repo as a no-op without sprinkling env-var checks everywhere.
    """

    def __init__(self):
        self._table_name = CIRCUIT_BREAKER_TABLE

    @property
    def enabled(self) -> bool:
        """True when CIRCUIT_BREAKER_TABLE is configured for this deployment."""
        return bool(self._table_name)

    @property
    def table(self):
        return dynamodb.Table(self._table_name)

    def query_open(self, limit: int = 100) -> list:
        """Return circuit breakers currently in OPEN state.

        Returns [] when the feature is disabled (no CIRCUIT_BREAKER_TABLE).
        """
        if not self.enabled:
            return []
        response = self.table.scan(
            FilterExpression=Attr('state').eq('OPEN'),
            ProjectionExpression='service_name, #s, last_failure_time',
            ExpressionAttributeNames={'#s': 'state'},
            Limit=limit,
        )
        return response.get('Items', [])


# Singleton instance
circuit_breakers_repo = CircuitBreakersRepo()
