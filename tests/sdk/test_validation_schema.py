"""Tests for polyris.validation — schema-aware checks.

Single-pipeline schema issues (duplicate column names) are caught by
`normalize_schema` and tested in `test_schema.py`. This file covers the
cross-pipeline checks done by `validate_schema_consistency` — the warnings
that surface when the same asset is declared by 2+ DAGs with conflicting
schemas.
"""
from __future__ import annotations

from polyris.validation import DAGInfo, validate_schema_consistency


def _dag(dag_id: str, **outlet_schemas) -> DAGInfo:
    """Tiny helper so each test can declare its DAGs in one line."""
    return DAGInfo(
        dag_id=dag_id,
        file_path=f"{dag_id}.py",
        outlet_schemas={k: v for k, v in outlet_schemas.items()},
    )


# ─── No conflict cases ──────────────────────────────────────────────────────

def test_no_warnings_when_no_outlets():
    assert validate_schema_consistency([]) == []


def test_no_warnings_when_only_one_pipeline_declares_asset():
    """A single declaration cannot conflict with itself."""
    dags = [_dag('p1', orders=[{'name': 'id', 'type': 'bigint'}])]
    assert validate_schema_consistency(dags) == []


def test_no_warnings_when_identical_schemas():
    """Two pipelines, same schema → no warning."""
    same = [
        {'name': 'id', 'type': 'bigint', 'primary_key': True},
        {'name': 'amount', 'type': 'decimal(10,2)'},
    ]
    dags = [_dag('p1', orders=same), _dag('p2', orders=same)]
    assert validate_schema_consistency(dags) == []


# ─── Type mismatch (most diagnosable case) ──────────────────────────────────

def test_type_mismatch_on_same_column_name_warns():
    dags = [
        _dag('p1', orders=[{'name': 'id', 'type': 'bigint'}]),
        _dag('p2', orders=[{'name': 'id', 'type': 'string'}]),
    ]
    warnings = validate_schema_consistency(dags)
    # Exactly one warning — the type mismatch (sizes are equal so no count warning).
    type_warnings = [w for w in warnings if 'type conflict' in w]
    assert len(type_warnings) == 1
    w = type_warnings[0]
    assert "'id'" in w
    assert "'bigint'" in w
    assert "'string'" in w
    assert 'p1' in w and 'p2' in w


def test_type_mismatch_warning_is_per_column():
    """Two columns conflict in the same asset → two warnings, one per column."""
    dags = [
        _dag('p1', orders=[
            {'name': 'a', 'type': 'bigint'},
            {'name': 'b', 'type': 'string'},
        ]),
        _dag('p2', orders=[
            {'name': 'a', 'type': 'string'},  # conflict 1
            {'name': 'b', 'type': 'bigint'},  # conflict 2
        ]),
    ]
    warnings = validate_schema_consistency(dags)
    type_warnings = [w for w in warnings if 'type conflict' in w]
    assert len(type_warnings) == 2
    column_names = {w.split("'")[3] for w in type_warnings}  # crude but works
    assert column_names == {'a', 'b'}


# ─── Column count mismatch ──────────────────────────────────────────────────

def test_different_column_counts_warns():
    dags = [
        _dag('p1', orders=[{'name': 'id', 'type': 'bigint'}]),
        _dag('p2', orders=[
            {'name': 'id', 'type': 'bigint'},
            {'name': 'amount', 'type': 'decimal(10,2)'},
        ]),
    ]
    warnings = validate_schema_consistency(dags)
    count_warnings = [w for w in warnings if 'different column counts' in w]
    assert len(count_warnings) == 1
    assert 'orders' in count_warnings[0]
    assert '1 columns' in count_warnings[0]
    assert '2 columns' in count_warnings[0]


def test_same_count_different_names_no_count_warning():
    """Same length, different column names — type-mismatch logic doesn't fire
    because the sets are disjoint, count-mismatch logic doesn't fire because
    counts are equal. We could add a third "disjoint columns" warning but
    that would over-warn for legitimate sub-schemas. No warning here."""
    dags = [
        _dag('p1', orders=[{'name': 'a', 'type': 'bigint'}]),
        _dag('p2', orders=[{'name': 'b', 'type': 'bigint'}]),
    ]
    assert validate_schema_consistency(dags) == []


# ─── Multi-pipeline + multi-asset ───────────────────────────────────────────

def test_warnings_only_emitted_for_conflicting_assets():
    """A pipeline-set with three assets, only one in conflict — only that
    asset's conflict shows up. Other assets are silent."""
    dags = [
        _dag('p1',
             a1=[{'name': 'id', 'type': 'bigint'}],
             a2=[{'name': 'x', 'type': 'string'}]),
        _dag('p2',
             a1=[{'name': 'id', 'type': 'string'}],  # conflict
             a3=[{'name': 'y', 'type': 'bigint'}]),
    ]
    warnings = validate_schema_consistency(dags)
    # Only 'a1' is in conflict → only a1 warnings.
    assert all('a1' in w for w in warnings)
    assert not any('a2' in w for w in warnings)
    assert not any('a3' in w for w in warnings)


def test_three_pipelines_all_distinct_types_one_warning():
    """Three pipelines, three different types on the same column. One
    warning, listing all three pipelines."""
    dags = [
        _dag('p1', t=[{'name': 'x', 'type': 'bigint'}]),
        _dag('p2', t=[{'name': 'x', 'type': 'string'}]),
        _dag('p3', t=[{'name': 'x', 'type': 'date'}]),
    ]
    warnings = [w for w in validate_schema_consistency(dags) if 'type conflict' in w]
    assert len(warnings) == 1
    w = warnings[0]
    for pipeline in ('p1', 'p2', 'p3'):
        assert pipeline in w
    for typ in ('bigint', 'string', 'date'):
        assert typ in w


# ─── Output is sorted (deterministic for snapshots and human reading) ──────

def test_warnings_sorted_by_asset_name():
    """Multiple assets in conflict → warnings sorted by asset name, so a
    user reading the list left-to-right sees alphabetical order. Also
    important for snapshot tests."""
    dags = [
        _dag('p1',
             zebra=[{'name': 'x', 'type': 'bigint'}],
             alpha=[{'name': 'x', 'type': 'bigint'}]),
        _dag('p2',
             zebra=[{'name': 'x', 'type': 'string'}],
             alpha=[{'name': 'x', 'type': 'string'}]),
    ]
    warnings = [w for w in validate_schema_consistency(dags) if 'type conflict' in w]
    # 'alpha' warning must come before 'zebra' warning.
    asset_order = [w.split("'")[1] for w in warnings]
    assert asset_order == ['alpha', 'zebra']
