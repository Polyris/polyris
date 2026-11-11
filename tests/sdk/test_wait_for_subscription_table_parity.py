"""Parity test: `wait_for` subscription writes must target a table whose
key schema matches the DAL code.

Currently `NotifyAssetSubscribersFunction` and `CheckAssetsFunction` receive
`SUBSCRIPTIONS_TABLE: !Ref AssetSubscriptionsTable` (schema PK=`asset_name`,
SK=`pipeline_name`), but their DAL writes and queries use
`dependency_key`/`subscriber` (schema of `DependencySubscriptionsTable`).
Every call hits `ValidationException` and is silently logged as a warning,
so tasks waiting on a not-yet-materialized asset hang to timeout.

Marked xfail until task 1.19 lands (narrow fix: repoint env var, or Option B:
rewrite DAL to match `AssetSubscriptionsTable` schema). On fix, drop the
xfail marker and this becomes a regression gate.
"""

import re
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).parent.parent.parent
TEMPLATE_PATH = REPO_ROOT / "sam" / "template.yaml"


@pytest.fixture(scope="module")
def template() -> dict:
    raw = TEMPLATE_PATH.read_text()
    # SAM !Ref / !Sub / !GetAtt are treated as opaque tags for structural parsing.
    class _Loader(yaml.SafeLoader):
        pass

    def _tag(_loader, node):
        if isinstance(node, yaml.ScalarNode):
            return {"__tag__": node.tag, "value": node.value}
        if isinstance(node, yaml.SequenceNode):
            return {"__tag__": node.tag, "value": _loader.construct_sequence(node)}
        return {"__tag__": node.tag, "value": _loader.construct_mapping(node)}

    for tag in ("!Ref", "!Sub", "!GetAtt", "!Join", "!ImportValue",
                "!FindInMap", "!If", "!Not", "!Equals", "!And", "!Or",
                "!Select", "!Split", "!Condition"):
        _Loader.add_constructor(tag, _tag)
    return yaml.load(raw, Loader=_Loader)


def _lambda_env(template: dict, logical_id: str) -> dict:
    """Return the Environment.Variables dict for a Lambda by logical id."""
    res = template["Resources"][logical_id]
    env = res["Properties"]["Environment"]["Variables"]
    return env


def _table_key_schema(template: dict, logical_id: str) -> tuple[str, str | None]:
    """Return (hash_key, range_key_or_None) for a DynamoDB table by logical id."""
    res = template["Resources"][logical_id]
    keys = res["Properties"]["KeySchema"]
    hash_key = next(k["AttributeName"] for k in keys if k["KeyType"] == "HASH")
    range_key = next(
        (k["AttributeName"] for k in keys if k["KeyType"] == "RANGE"), None
    )
    return hash_key, range_key


@pytest.mark.xfail(
    reason="task 1.19: SUBSCRIPTIONS_TABLE env var points at a table whose schema "
           "does not match the DAL's dependency_key/subscriber composite key",
    strict=True,
    raises=AssertionError,
)
def test_notify_asset_subscribers_env_matches_dal_schema(template):
    env = _lambda_env(template, "NotifyAssetSubscribersFunction")
    tag = env["SUBSCRIPTIONS_TABLE"]
    assert tag["__tag__"] == "!Ref", f"expected !Ref, got {tag['__tag__']}"
    table_id = tag["value"]
    hash_key, range_key = _table_key_schema(template, table_id)
    # DAL (sam/lambdas/notify_asset_subscribers/dal/__init__.py) uses:
    #   PK: dependency_key, SK: subscriber
    assert (hash_key, range_key) == ("dependency_key", "subscriber"), (
        f"{table_id} has schema ({hash_key}, {range_key}); "
        f"DAL expects (dependency_key, subscriber)"
    )


@pytest.mark.xfail(
    reason="task 1.19: SUBSCRIPTIONS_TABLE env var points at a table whose schema "
           "does not match the DAL's dependency_key/subscriber composite key",
    strict=True,
    raises=AssertionError,
)
def test_check_assets_env_matches_dal_schema(template):
    env = _lambda_env(template, "CheckAssetsFunction")
    tag = env["SUBSCRIPTIONS_TABLE"]
    assert tag["__tag__"] == "!Ref", f"expected !Ref, got {tag['__tag__']}"
    table_id = tag["value"]
    hash_key, range_key = _table_key_schema(template, table_id)
    assert (hash_key, range_key) == ("dependency_key", "subscriber"), (
        f"{table_id} has schema ({hash_key}, {range_key}); "
        f"DAL expects (dependency_key, subscriber)"
    )
