"""Deploy a CloudFormation template through a change set (ADR #131).

Shared by ``polyris-deploy`` (pipeline stacks) and ``polyris-up`` (the
platform stack), so neither needs the ``aws`` CLI: everything goes through
boto3. Also hosts the stack-event narration both commands print while
CloudFormation works.
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

from botocore.exceptions import ClientError, WaiterError

from ._ansi import CYAN, GREEN, RED, RESET, YELLOW

# CloudFormation's TemplateBody limit; larger templates must go through S3.
TEMPLATE_BODY_LIMIT = 51_200

# A FAILED change set with one of these reasons just means "nothing to do".
_NO_CHANGES_MARKERS = ("didn't contain changes", "No updates are to be performed")

STACK_WAIT = {"Delay": 10, "MaxAttempts": 360}  # up to an hour, like the console
_CHANGE_SET_MAX_WAIT_S = 600


class ChangeSetError(Exception):
    """CloudFormation refused or failed the change; the message says why."""


def describe_stack(cfn, stack_name: str) -> Optional[Dict[str, Any]]:
    """The describe_stacks entry for ``stack_name``, or None if it doesn't exist."""
    try:
        stacks = cfn.describe_stacks(StackName=stack_name)["Stacks"]
    except ClientError as e:
        if "does not exist" in str(e):
            return None
        raise
    return stacks[0] if stacks else None


def all_stacks(cfn) -> List[Dict[str, Any]]:
    """Every stack in the region (all describe_stacks pages)."""
    stacks: List[Dict[str, Any]] = []
    for page in cfn.get_paginator("describe_stacks").paginate():
        stacks.extend(page.get("Stacks", []))
    return stacks


def stack_status(cfn, stack_name: str) -> Optional[str]:
    """The stack's status, "" if CloudFormation didn't report one, None if it doesn't exist."""
    stack = describe_stack(cfn, stack_name)
    return None if stack is None else stack.get("StackStatus", "")


def create_change_set(
    cfn,
    *,
    stack_name: str,
    capabilities: Iterable[str],
    template_body: Optional[str] = None,
    template_url: Optional[str] = None,
    parameters: Iterable[Dict[str, Any]] = (),
    tags: Optional[List[Dict[str, str]]] = None,
    poll_interval: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Optional[Dict[str, Any]]:
    """Create a change set for ``stack_name`` and wait until it is ready.

    Picks CREATE or UPDATE from the stack's current state. Returns the
    described change set, or None when the template and parameters change
    nothing (the change set is deleted again). Raises ChangeSetError for a
    stack that can't be updated or a change set that fails.
    """
    if (template_body is None) == (template_url is None):
        raise ValueError("Pass exactly one of template_body / template_url")
    status = stack_status(cfn, stack_name)
    if status == "ROLLBACK_COMPLETE":
        raise ChangeSetError(
            f"Stack {stack_name} is in ROLLBACK_COMPLETE (its first create failed) and can't be updated. "
            f"Delete it, then deploy again."
        )
    if status and status.endswith("_IN_PROGRESS") and status != "REVIEW_IN_PROGRESS":
        raise ChangeSetError(f"Stack {stack_name} is busy ({status}). Try again when it finishes.")
    change_set_type = "CREATE" if status in (None, "REVIEW_IN_PROGRESS") else "UPDATE"

    name = f"polyris-{int(time.time() * 1000)}"
    kwargs: Dict[str, Any] = {
        "StackName": stack_name,
        "ChangeSetName": name,
        "ChangeSetType": change_set_type,
        "Capabilities": list(capabilities),
        "Parameters": list(parameters),
    }
    if template_body is not None:
        kwargs["TemplateBody"] = template_body
    else:
        kwargs["TemplateURL"] = template_url
    if tags is not None:
        kwargs["Tags"] = tags
    cfn.create_change_set(**kwargs)

    waited = 0.0
    while True:
        cs = cfn.describe_change_set(StackName=stack_name, ChangeSetName=name)
        state = cs.get("Status")
        if state == "CREATE_COMPLETE":
            cs.setdefault("ChangeSetName", name)
            cs["ChangeSetType"] = change_set_type
            return cs
        if state == "FAILED":
            reason = cs.get("StatusReason", "")
            if any(marker in reason for marker in _NO_CHANGES_MARKERS):
                cfn.delete_change_set(StackName=stack_name, ChangeSetName=name)
                return None
            try:  # a failed change set is clutter on the stack; removing it is best-effort
                cfn.delete_change_set(StackName=stack_name, ChangeSetName=name)
            except ClientError as e:
                print(f"  (could not delete failed change set {name}: {e})")
            raise ChangeSetError(f"Change set failed: {reason}")
        if waited >= _CHANGE_SET_MAX_WAIT_S:
            raise ChangeSetError(f"Change set {name} still {state} after {_CHANGE_SET_MAX_WAIT_S}s.")
        sleep(poll_interval)
        waited += poll_interval


def execute_change_set(cfn, *, stack_name: str, change_set: Dict[str, Any]) -> None:
    """Execute a ready change set and block until the stack settles."""
    cfn.execute_change_set(StackName=stack_name, ChangeSetName=change_set["ChangeSetName"])
    waiter_name = "stack_create_complete" if change_set.get("ChangeSetType") == "CREATE" else "stack_update_complete"
    try:
        cfn.get_waiter(waiter_name).wait(StackName=stack_name, WaiterConfig=STACK_WAIT)
    except WaiterError as e:
        raise ChangeSetError(
            f"Stack {stack_name} did not finish: {e}. The events above (or the CloudFormation console) "
            f"name the failing resource."
        )


def start_event_watcher(cfn, stack_name: str) -> Callable[[], None]:
    """Start printing new stack events in the background; returns a stop() callable.

    Seeds with the events that already exist, so an update doesn't replay a
    previous deploy's history as if it were happening now.
    """
    seen: set = set()
    try:
        seen.update(e["EventId"] for e in cfn.describe_stack_events(StackName=stack_name)["StackEvents"])
    except Exception:  # noqa: BLE001 — narration only; a fresh stack has no events yet
        pass
    stop = threading.Event()
    thread = threading.Thread(target=_watch_stack_events, args=(cfn, stack_name, seen, stop), daemon=True)
    thread.start()

    def _stop() -> None:
        stop.set()
        thread.join(timeout=5)
    return _stop


def _colorize_status(status: str) -> str:
    """Color a CloudFormation ResourceStatus by outcome: red for
    failed/rollback, green for complete, cyan for in-progress, yellow for
    anything else (e.g. REVIEW_IN_PROGRESS). Plain text if stdout isn't a
    real terminal (checked at call time, not cached, so tests can patch
    sys.stdout.isatty directly)."""
    if not sys.stdout.isatty():
        return status
    if "FAILED" in status or "ROLLBACK" in status:
        color = RED
    elif "COMPLETE" in status:
        color = GREEN
    elif "IN_PROGRESS" in status:
        color = CYAN
    else:
        color = YELLOW
    return f"{color}{status}{RESET}"


def _watch_stack_events(
    cfn_client,
    stack_name: str,
    seen_event_ids: set,
    stop_event: threading.Event,
    poll_interval: float = 2.0,
) -> None:
    """Print new CloudFormation stack events as they happen, until
    stop_event is set.

    Purely observational — only reads describe_stack_events; never raises,
    so a polling hiccup (or the stack not existing yet, on a fresh CREATE)
    can never affect the actual deploy outcome, which is driven entirely by
    the change set this runs alongside.

    `seen_event_ids` must already be seeded (by the caller) with any events
    that existed BEFORE this deploy started — CloudFormation keeps full
    event history per stack across past deploys, so without seeding, the
    first poll here would print old, unrelated events from a previous
    deploy as if they were happening right now.
    """
    while not stop_event.is_set():
        try:
            events = cfn_client.describe_stack_events(StackName=stack_name)["StackEvents"]
            new_events = [e for e in events if e["EventId"] not in seen_event_ids]
            new_events.sort(key=lambda e: e["Timestamp"])
            for e in new_events:
                seen_event_ids.add(e["EventId"])
                reason = f" — {e['ResourceStatusReason']}" if e.get("ResourceStatusReason") else ""
                status_colored = _colorize_status(e["ResourceStatus"])
                # Pad the RAW status (not the color-escaped string) so
                # ANSI codes don't throw off the column alignment.
                padding = " " * max(0, 28 - len(e["ResourceStatus"]))
                print(f"    {status_colored}{padding} {e['ResourceType']:<32} {e['LogicalResourceId']}{reason}")

        except Exception as e:
            # Deliberately broad, and deliberately not narrowed to
            # (ClientError, BotoCoreError): this is a daemon *narration* thread
            # wrapping both the AWS call and the event formatting below it. The
            # deploy's correctness does not depend on it, but a traceback from
            # it mid-deploy — or a dead poller — is worse than a skipped tick.
            # ADR #28's narrow rule is for paths whose failure means something;
            # this one's does not. What Principle #11 forbids is the *silent*
            # swallow this used to be (`pass`, no comment), so it now says so.
            #
            # "does not exist" is expected on the first few polls of a CREATE:
            # CloudFormation hasn't registered the stack yet. Suppress that
            # specific case to avoid confusing "progress poll skipped" output.
            if "does not exist" not in str(e):
                print(f"    (progress poll skipped: {e})")
        stop_event.wait(poll_interval)


