"""The wake contract between the coordination Lambdas and the state machines (ADR #130 §3, §6).

The dependency wrapper routes on the signal names. The registration helper
fails a wait with COORDINATION_ERROR, a name the wrapper's Retry never
matches. The state machines retry a coordination Lambda on TRANSIENT_ERROR,
the error name it reports for a transient failure. Contract tests compare
these values with the templates.
"""

DEPS_READY = "deps_ready"
DEPS_SKIP = "deps_skip"
DEPS_BLOCKED = "deps_blocked"
SIGNALS = frozenset({DEPS_READY, DEPS_SKIP, DEPS_BLOCKED})

COORDINATION_ERROR = "CoordinationError"
TRANSIENT_ERROR = "CoordinationTransient"

#: Step Functions error codes for a task token that can no longer be signalled:
#: its wait already ended (another signal, a timeout) or its execution is gone.
DEAD_TOKEN_CODES = frozenset({"TaskTimedOut", "InvalidToken", "TaskDoesNotExist"})
