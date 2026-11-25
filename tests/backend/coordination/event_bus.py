"""The default EventBridge bus as one rule of the template delivers to its target (ADR #137 §7).

The interpreter records the *Execution Status Change* event of each end of a
Standard execution (`Runtime.events`). `EventBus.for_rule` reads a rule's
`EventPattern` and its one target from `sam/template.yaml`, resolved as
CloudFormation resolves them, matches each new event with `stream_filters`
and invokes the target's in-process handler, as EventBridge invokes a Lambda
asynchronously: a handler that raises is invoked twice more, then the event
is dropped. Delivery is best effort on AWS: `drop` loses matching events,
and `publish` sends one of the test's own, such as an event that comes twice
or one of another platform. `test_event_bus.py` pins each.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from stream_filters import matches

Event = Dict[str, Any]
#: An asynchronous invocation that fails is tried twice more, then dropped (Lambda's default).
ASYNC_ATTEMPTS = 3


@dataclass
class _Fault:
    matches: Callable[[Event], bool]
    left: Optional[int]


@dataclass
class EventBus:
    runtime: Any
    pattern: Dict[str, Any]
    function: str                                  # the target's function name
    delivered: List[Event] = field(default_factory=list)
    filtered: List[Event] = field(default_factory=list)
    lost: List[Event] = field(default_factory=list)
    failed: List[Event] = field(default_factory=list)
    results: List[Any] = field(default_factory=list)
    _read: int = 0
    _drops: List[_Fault] = field(default_factory=list)

    @classmethod
    def for_rule(cls, stack: Any, logical: str) -> "EventBus":
        """The rule `logical` of the stack's template, with its pattern and its one Lambda target."""
        props = stack.resources[logical]["Properties"]
        if props.get("State", "ENABLED") != "ENABLED":
            raise AssertionError(f"{logical} is not enabled")
        pattern = json.loads(stack._sub(props["EventPattern"]))
        [target] = props["Targets"]
        arn = stack._resolve(target["Arn"])
        return cls(stack.runtime, pattern, arn.rsplit(":", 1)[-1])

    def drop(self, matches_: Callable[[Event], bool], *, times: Optional[int] = 1) -> None:
        """Lose the matching events before the rule's target gets them."""
        self._drops.append(_Fault(matches_, times))

    def heal(self) -> None:
        """No more events are lost (World.run_to_the_end); a lost one stays lost."""
        self._drops.clear()

    def deliver(self) -> int:
        """Deliver every event the runtime recorded since the last call; return how many reached the target."""
        new, self._read = self.runtime.events[self._read:], len(self.runtime.events)
        return sum(self.publish(event) for event in new)

    def publish(self, event: Event) -> bool:
        """One event on the bus: whether the rule matched it and its target got it."""
        if not matches(self.pattern, event):
            self.filtered.append(event)
            return False
        for fault in self._drops:
            if fault.left != 0 and fault.matches(event):
                if fault.left is not None:
                    fault.left -= 1
                self.lost.append(event)
                return False
        handler = self.runtime.lambdas[self.function]
        for attempt in range(ASYNC_ATTEMPTS):
            try:
                self.results.append(handler(event, None))
                break
            except Exception:  # noqa: BLE001 — as Lambda retries an asynchronous invocation that fails
                if attempt + 1 == ASYNC_ATTEMPTS:
                    self.failed.append(event)
                    return False
        self.delivered.append(event)
        return True
