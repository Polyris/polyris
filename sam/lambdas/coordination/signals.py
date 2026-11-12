"""Signals to waiting wrappers (ADR #130 §3).

The wrapper reads ``signal`` and ``reason`` from the JSON it is resumed with:
``deps_skip`` and ``deps_blocked`` route to its skip and blocked paths, any
other value to ``deps_ready``.
"""

import json
from typing import Any, Dict, Optional

from botocore.exceptions import BotoCoreError, ClientError

from failures import is_dead_token, reraise_transient

ACCEPTED = "accepted"
DEAD = "dead"

#: Error name sent with SendTaskFailure. Never a `States.*` name: the
#: wrapper's Retry matches States.Timeout and would re-register the task.
COORDINATION_ERROR = "CoordinationError"
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
        """Fail the wrapper's wait with CoordinationError. Returns ACCEPTED or DEAD."""
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
