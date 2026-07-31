"""DDL parity test — Python side (ADR #46).

Loads the shared fixture file (`tests/fixtures/ddl_parity.json`) and
asserts that `polyris.assets._render_glue_ddl` produces byte-identical
output to the canonical `expected` strings.

The same fixture file is loaded by
`ui/src/utils/__tests__/ddl-glue-parity.test.ts` on the TypeScript side.
If either renderer drifts from the fixture, exactly one of the two parity
tests fails — pinpointing which side moved without ambiguity.

How to update fixtures: edit the renderer (Python or TS), regenerate the
fixture by running `tools/regen_ddl_parity.py` (or by hand-editing the
JSON file), and confirm both parity tests pass. The fixture is the
contract — both renderers must conform to it.
"""
import json
from pathlib import Path

import pytest

from polyris import Asset, Column, types as t
from polyris.assets import _render_glue_ddl


# Resolve the fixture relative to the repo root regardless of where pytest
# is invoked from. Path goes: tests/sdk/<this file> -> tests/sdk/ -> tests/
# -> tests/fixtures/ddl_parity.json
FIXTURE_PATH = Path(__file__).resolve().parent.parent / 'fixtures' / 'ddl_parity.json'


def _load_fixtures():
    with FIXTURE_PATH.open() as f:
        return json.load(f)


def _build_asset(input_dict: dict) -> Asset:
    """Reconstruct an Asset from the fixture's `input` block.

    The fixture stores schema as list of dicts (matching the wire format
    used in pipeline_registry); we map each back to a Column instance so
    the renderer sees the same structure as it does in production.
    """
    columns = [
        Column(
            name=col['name'],
            type=t.type_from_string(col['type']),
            partition_key=col.get('partition_key', False),
            nullable=col.get('nullable', True),
            description=col.get('description'),
        )
        for col in input_dict['schema']
    ]
    return Asset(
        name=input_dict['assetName'],
        glue_table=input_dict.get('glueTable', ''),
        description=input_dict.get('description', ''),
        uri=input_dict.get('uri', ''),
        schema=columns,
    )


@pytest.mark.parametrize('fixture', _load_fixtures(), ids=lambda f: f['name'])
def test_render_glue_ddl_matches_fixture(fixture):
    """Each fixture's expected DDL must equal what `_render_glue_ddl` produces.

    Failure here means the Python renderer has changed without updating the
    fixture — either the change is intentional (regenerate the fixture and
    update the TS mirror to match) or it's a regression (revert).
    """
    asset = _build_asset(fixture['input'])
    actual = _render_glue_ddl(asset)
    expected = fixture['expected']
    assert actual == expected, (
        f"Fixture {fixture['name']!r} mismatch.\n"
        f"--- expected ---\n{expected}\n"
        f"--- actual ---\n{actual}\n"
    )


def test_to_ddl_dispatcher_calls_glue_renderer():
    """`Asset.to_ddl()` is a thin dispatcher that delegates to the
    per-dialect renderer. Verify the dispatch path: same Asset run
    through both gives the same string."""
    asset = Asset(
        'orders',
        glue_table='analytics.orders',
        schema=[
            Column('id', t.bigint()),
            Column('amount', t.decimal(10, 2)),
        ],
    )
    assert asset.to_ddl() == _render_glue_ddl(asset)
    assert asset.to_ddl(dialect='glue') == _render_glue_ddl(asset)


def test_to_ddl_rejects_unsupported_dialect():
    asset = Asset('x', schema=[Column('a', t.string())])
    with pytest.raises(ValueError, match="only 'glue' dialect is currently supported"):
        asset.to_ddl(dialect='bigquery')


def test_to_ddl_rejects_empty_schema():
    asset = Asset('x', schema=[])
    with pytest.raises(ValueError, match="has no schema declared"):
        asset.to_ddl()
