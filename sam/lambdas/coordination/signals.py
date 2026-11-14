"""Signals to waiting wrappers (ADR #130 §3).

The wrapper reads ``signal`` and ``reason`` from the JSON it is resumed with:
``deps_skip`` and ``deps_blocked`` route to its skip and blocked paths, any
other value to ``deps_ready``.
"""

import json
from typing import Any, Dict, Optional

from botocore.exceptions import BotoCoreError, ClientError

from polyris.coordination.wake import COORDINATION_ERROR
from failures import is_dead_token, reraise_transient

ACCEPTED = "accepted"
DEAD = "dead"

_CAUSE_LIMIT = 2000


class Signals:
    def __init__(self, client: Any):
        self.client = client

    def succeed(self, token: str, payload: Dict[str, Any]) -> str:
        """Resume the wrapper. Returns ACCEPTED or DEAD; raises on anything else."""
        try:
            self.client.send_task_success(taskToken=token, output=json.dumps(payload))
        except (ClientError, BotoCoreError) as exc:
            if is_dead_token(exc):
                return DEAD
            reraise_transient(exc)
            raise
        return ACCEPTED

    def fail(self, token: str, cause: str) -> str:
        """Fail a wait with CoordinationError: a wrapper's wait for its dependencies, or a
        run's wait for a task the reconciler closed (ADR #135). Returns ACCEPTED or DEAD."""
        try:
            self.client.send_task_failure(
                taskToken=token, error=COORDINATION_ERROR, cause=cause[:_CAUSE_LIMIT]
            )
        except (ClientError, BotoCoreError) as exc:
            if is_dead_token(exc):
                return DEAD
            reraise_transient(exc)
            raise
        return ACCEPTED

    def execution_status(self, execution_arn: str) -> Optional[str]:
        """``RUNNING``, ``SUCCEEDED``, ...; None when the execution does not exist."""
        try:
            return str(self.client.describe_execution(executionArn=execution_arn)["status"])
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ExecutionDoesNotExist":
                return None
            reraise_transient(exc)
            raise
