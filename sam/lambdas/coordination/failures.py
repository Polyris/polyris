"""How coordination classifies AWS errors (ADR #130 §3).

* transient — retrying the same call can succeed: nothing is written and the
  caller (a stream retry, an SFN `Retry`, the sweeper) repeats the call;
* dead token — the task token can no longer be signalled;
* condition failed — another owner already moved the row: not an error;
* everything else is non-transient and must surface (the task fails with
  the real cause, or a warning is written).
"""

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from coordination_core.wake import DEAD_TOKEN_CODES

TRANSIENT_CODES = frozenset({
    "ThrottlingException",
    "Throttling",
    "ProvisionedThroughputExceededException",
    "RequestLimitExceeded",
    "TooManyRequestsException",
    "TransactionConflictException",
    "InternalServerError",
    "InternalFailure",
    "ServiceUnavailable",
    "ServiceUnavailableException",
    "RequestTimeout",
    "RequestTimeoutException",
})
CONDITION_FAILED = "ConditionalCheckFailedException"

_TRANSIENT_NETWORK = (EndpointConnectionError, ConnectionClosedError, ConnectTimeoutError, ReadTimeoutError)


class CoordinationTransient(Exception):
    """A transient failure. Nothing was written; the caller should retry.

    Lambda reports it under its class name, which the state machines' Retry
    matches: the name must stay equal to coordination_core.wake.TRANSIENT_ERROR.
    """


def error_code(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", ""))
    return ""


def is_transient(exc: BaseException) -> bool:
    """A listed code, or any server-side failure (HTTP 5xx) whose code the list does not know yet."""
    if isinstance(exc, ClientError):
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return error_code(exc) in TRANSIENT_CODES or (isinstance(status, int) and status >= 500)
    return isinstance(exc, _TRANSIENT_NETWORK)


def is_dead_token(exc: BaseException) -> bool:
    return error_code(exc) in DEAD_TOKEN_CODES


def is_condition_failed(exc: BaseException) -> bool:
    return error_code(exc) == CONDITION_FAILED


def reraise_transient(exc: BaseException) -> None:
    """Re-raise ``exc`` as :class:`CoordinationTransient` when it is transient."""
    if isinstance(exc, (ClientError, BotoCoreError)) and is_transient(exc):
        raise CoordinationTransient(f"{type(exc).__name__}: {exc}") from exc
