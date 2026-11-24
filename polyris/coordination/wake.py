"""The error names the coordination Lambdas and the state machines share (ADR #137 §1, §4).

A state machine retries a coordination Lambda on TRANSIENT_ERROR, the error name
it reports for a transient failure (``Start_Run``'s Retry). Step Functions
answers DEAD_TOKEN_CODES for a task token that can no longer be signalled: its
wait ended or its execution is gone, which is no failure of the answer (ADR #135),
so nothing retries on them. Contract tests compare these values with the
templates and the Lambdas.
"""

TRANSIENT_ERROR = "CoordinationTransient"

#: Step Functions error codes for a task token that can no longer be signalled:
#: its wait already ended (another signal, a timeout) or its execution is gone.
DEAD_TOKEN_CODES = frozenset({"TaskTimedOut", "InvalidToken", "TaskDoesNotExist"})
