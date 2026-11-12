"""Evaluate the JSONata of a state-machine template from a contract test.

Templates are loaded with their `${...}` substitutions replaced by test
values, and expressions are evaluated with jsonata-python, with the
functions Step Functions adds ($parse, $hash) and a fixed $now().
tests/backend/coordination runs whole templates; this module is for
pinning single expressions against the Python code they must agree with.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from polyris.coordination.times import format_utc

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)


def load_template(relative_path: str, substitutions: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A template under sam/sfn_templates; unknown `${...}` placeholders become "0"."""
    values = substitutions or {}
    text = (REPO_ROOT / "sam" / "sfn_templates" / relative_path).read_text()
    return json.loads(re.sub(r"\$\{(\w+)\}", lambda m: str(values.get(m.group(1), "0")), text))


def find_state(states: Dict[str, Any], name: str) -> Dict[str, Any]:
    """A state by name, searched through Parallel branches and Map processors."""
    for key, state in states.items():
        if key == name:
            return state
        nested = [b["States"] for b in state.get("Branches", [])]
        processor = state.get("ItemProcessor") or state.get("Iterator")
        if processor:
            nested.append(processor["States"])
        for inner in nested:
            try:
                return find_state(inner, name)
            except KeyError:
                pass
    raise KeyError(name)


def evaluate(value: Any, *, input: Any = None, context: Optional[Dict[str, Any]] = None,
             variables: Optional[Dict[str, Any]] = None, now: datetime = NOW) -> Any:
    """Evaluate every `{% ... %}` string in `value` as Step Functions would."""
    jsonata = pytest.importorskip("jsonata")
    if isinstance(value, dict):
        return {k: evaluate(v, input=input, context=context, variables=variables, now=now) for k, v in value.items()}
    if isinstance(value, list):
        return [evaluate(v, input=input, context=context, variables=variables, now=now) for v in value]
    if not (isinstance(value, str) and value.strip().startswith("{%") and value.strip().endswith("%}")):
        return value
    compiled = jsonata.Jsonata(value.strip()[2:-2].strip())
    compiled.assign("states", {"input": input, "context": context or {}})
    for name, val in (variables or {}).items():
        compiled.assign(name, val)
    compiled.register_lambda("now", lambda: format_utc(now))
    compiled.register_lambda("parse", json.loads)
    compiled.register_lambda("hash", lambda text, alg: hashlib.new(
        str(alg).replace("-", "").lower(), str(text).encode("utf-8")).hexdigest())
    return compiled.evaluate(None)


def plain(item: Dict[str, Any]) -> Dict[str, Any]:
    """A DynamoDB-typed item ({"S": ...}, {"N": ...}) as plain Python values."""
    from boto3.dynamodb.types import TypeDeserializer

    deserializer = TypeDeserializer()
    return {k: deserializer.deserialize(v) for k, v in item.items()}
