#!/usr/bin/env python3
"""Regenerate tests/fixtures/ddl_parity.json from the current SDK output.

This fixture is the parity contract between the Python `_render_glue_ddl`
helper and the TypeScript `renderGlueDDL` function (UI). Both renderers
must produce byte-identical output for every fixture's `expected` field.
See ADR #46 for the design.

When to run this script:
  - You changed `_render_glue_ddl` in `polyris/assets.py` and want the
    fixture to reflect the new output. After regenerating, update the
    TypeScript mirror in `ui/src/utils/ddl-glue.ts` to match — both
    parity tests must pass before merging.
  - You added a new fixture case to cover a renderer branch that
    existing cases didn't exercise.
  - You removed a fixture case that no longer represents a meaningful
    state.

When NOT to run this script:
  - As a "test fix" when a parity test is failing. If parity is broken,
    one of the two renderers drifted from the contract — figure out
    which one and update the renderer, not the fixture.

Usage:
    python3 tools/regen_ddl_parity.py

The script writes to `tests/fixtures/ddl_parity.json` (relative to the
repo root) and prints a summary of the fixtures it generated.

To add a new fixture case, edit the `_build_fixtures()` function below.
Each case has a `name` (used as test ID), a `description_text` (used as
the test message — what does this case lock down?), and an `Asset` whose
`to_ddl()` output is recorded as the canonical expected string.
"""
import json
import sys
from pathlib import Path

# Allow running from anywhere — script resolves repo root from its own location.
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

from polyris import Asset, Column, types as t
from polyris.assets import _render_glue_ddl


def _col_to_dict(c: Column) -> dict:
    """Serialize a Column to the wire shape the UI sees from the backend."""
    return {
        'name': c.name,
        'type': t.to_glue_string(c.type),
        'partition_key': c.partition_key,
        'nullable': c.nullable,
        'description': c.description,
    }


def _fixture(name: str, description_text: str, asset: Asset) -> dict:
    """Build one fixture entry: input descriptor + expected DDL."""
    return {
        'name': name,
        'description_text': description_text,
        'input': {
            'assetName': asset.name,
            'glueTable': asset.glue_table,
            'description': asset.description or '',
            'uri': asset.uri or '',
            'schema': [_col_to_dict(c) for c in asset.schema],
        },
        'expected': _render_glue_ddl(asset),
    }


def _build_fixtures() -> list:
    """Six canonical cases that lock down every renderer branch.

    Adding a new case: append another `_fixture(...)` call. Each case
    should exercise a *distinct* renderer branch — duplicate coverage
    bloats the fixture without adding signal.
    """
    return [
        _fixture(
            'simple',
            'No partitions/description/URI — minimal CREATE TABLE',
            Asset('orders', glue_table='analytics.orders', schema=[
                Column('id', t.bigint()),
                Column('amount', t.decimal(10, 2)),
            ]),
        ),
        _fixture(
            'with_partition',
            'Partition column extracted into PARTITIONED BY',
            Asset('events', glue_table='analytics.events', schema=[
                Column('event_id', t.string()),
                Column('payload', t.string()),
                Column('event_date', t.date(), partition_key=True),
            ]),
        ),
        _fixture(
            'with_description',
            "Asset COMMENT + column COMMENT, single quotes doubled",
            Asset(
                'users',
                glue_table='analytics.users',
                description="Master user table — Mike's working copy",
                schema=[
                    Column('id', t.bigint(), description="User's primary key"),
                    Column('email', t.string(), description='Verified email; nullable'),
                ],
            ),
        ),
        _fixture(
            'with_uri',
            's3:// URI emitted as LOCATION',
            Asset(
                'inventory',
                glue_table='analytics.inventory',
                uri='s3://my-lake/inventory/',
                schema=[
                    Column('sku', t.string()),
                    Column('quantity', t.integer()),
                ],
            ),
        ),
        _fixture(
            'bare_name',
            'No glue_table → asset name in backticks',
            Asset('retail/orders', schema=[
                Column('id', t.bigint()),
                Column('total', t.decimal(10, 2)),
            ]),
        ),
        _fixture(
            'all_features',
            'Partition + description + URI — full exercise',
            Asset(
                'retail/sales',
                glue_table='retail.sales',
                description='Daily aggregated sales per SKU',
                uri='s3a://lake/retail/sales/',
                schema=[
                    Column('sku', t.string(), description='Product SKU'),
                    Column('total_amount', t.decimal(12, 2), description='Sum of line item totals'),
                    Column('event_date', t.date(), partition_key=True, description='UTC date'),
                ],
            ),
        ),
    ]


def main() -> int:
    fixtures = _build_fixtures()
    out_path = REPO_ROOT / 'tests' / 'fixtures' / 'ddl_parity.json'
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open('w') as f:
        json.dump(fixtures, f, indent=2, ensure_ascii=False)

    print(f"Wrote {len(fixtures)} fixtures to {out_path.relative_to(REPO_ROOT)}:")
    for fix in fixtures:
        print(f"  - {fix['name']}: {fix['description_text']}")
    print()
    print("Next steps:")
    print("  1. Run the Python parity test:")
    print("     python3 -m pytest tests/sdk/test_ddl_parity.py -v")
    print("  2. Run the TypeScript parity test:")
    print("     cd ui && npx vitest run src/utils/ddl-glue-parity.test.ts")
    print("  3. If TS fails, update ui/src/utils/ddl-glue.ts to match.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
