"""Asset combinator / reference operator tests.

Closes the remaining pure-logic branches in ``polyris.assets`` (CLAUDE.md #13):
the ``&`` / ``|`` / ``==`` / ``repr`` / ``to_dict`` / ``uri`` paths on AssetRef,
AssetConsecutiveRef, AssetAll, AssetAny and AssetAlias, plus the list-form
handling in ``normalize_asset_schedule``. The remaining uncovered lines are the
``from_*`` factory constructors, which require pyiceberg / AWS / parquet I/O and
are ``# pragma: no cover``.
"""
from __future__ import annotations

import pytest

from polyris.assets import (
    Asset,
    AssetRef,
    AssetAll,
    AssetAny,
    AssetAlias,
    Metadata,
    normalize_asset_schedule,
)

a = Asset("ns/a")
b = Asset("ns/b")
c = Asset("ns/c")


# ============================================================ #
# Metadata + Asset construction edges
# ============================================================ #
class TestMetadataAndAssetInit:
    def test_metadata_to_dict(self):
        d = Metadata(asset=a, data={"row_count": 5}).to_dict()
        assert d == {"asset_name": "ns/a", "metadata": {"row_count": 5}}

    def test_metadata_alias_for_extra(self):
        asset = Asset("ns/m", metadata={"k": "v"})
        assert asset.extra == {"k": "v"}

    def test_metadata_and_extra_merge(self):
        asset = Asset("ns/m2", metadata={"k": "from_meta", "only_meta": 1}, extra={"k": "from_extra"})
        assert asset.extra["k"] == "from_extra"   # extra wins
        assert asset.extra["only_meta"] == 1

    def test_uri_passed_as_first_positional(self):
        asset = Asset("s3://bucket/path")
        assert asset.uri == "s3://bucket/path"

    def test_no_name_or_uri_raises(self):
        with pytest.raises(ValueError):
            Asset()


# ============================================================ #
# AssetRef operators
# ============================================================ #
class TestAssetRef:
    def test_repr_without_freshness(self):
        assert repr(AssetRef(asset=a)) == "AssetRef('ns/a')"

    def test_repr_with_freshness(self):
        assert "within=24h" in repr(a.within(hours=24))

    def test_and_with_asset(self):
        result = a.within(hours=24) & b
        assert isinstance(result, AssetAll) and len(result.assets) == 2

    def test_and_with_assetall_merges(self):
        result = a.within(hours=24) & (b & c)
        assert isinstance(result, AssetAll) and len(result.assets) == 3

    def test_or_with_asset(self):
        result = a.within(hours=24) | b
        assert isinstance(result, AssetAny) and len(result.assets) == 2

    def test_or_with_assetany_merges(self):
        result = a.within(hours=24) | (b | c)
        assert isinstance(result, AssetAny) and len(result.assets) == 3

    def test_asset_or_assetany_merges(self):
        result = a | (b | c)   # Asset.__or__ with an AssetAny operand
        assert isinstance(result, AssetAny) and len(result.assets) == 3

    def test_and_bad_type_raises(self):
        with pytest.raises(TypeError):
            a.within(hours=24) & 42

    def test_or_bad_type_raises(self):
        with pytest.raises(TypeError):
            a.within(hours=24) | 42


# ============================================================ #
# AssetConsecutiveRef operators
# ============================================================ #
class TestAssetConsecutiveRef:
    def test_equality(self):
        assert a.consecutive(days=3) == a.consecutive(days=3)
        assert a.consecutive(days=3) != a.consecutive(days=5)
        assert a.consecutive(days=3) != "nope"

    def test_uri_delegates(self):
        assert a.consecutive(days=3).uri == a.uri

    def test_and_with_asset(self):
        assert isinstance(a.consecutive(days=3) & b, AssetAll)

    def test_and_with_assetall_merges(self):
        result = a.consecutive(days=3) & (b & c)
        assert isinstance(result, AssetAll) and len(result.assets) == 3

    def test_or_with_asset(self):
        assert isinstance(a.consecutive(days=3) | b, AssetAny)

    def test_or_with_assetany_merges(self):
        result = a.consecutive(days=3) | (b | c)
        assert isinstance(result, AssetAny) and len(result.assets) == 3

    def test_and_bad_type_raises(self):
        with pytest.raises(TypeError):
            a.consecutive(days=3) & 42


# ============================================================ #
# AssetAll / AssetAny
# ============================================================ #
class TestAssetCollections:
    def test_assetany_or_merges(self):
        result = (a | b) | c
        assert isinstance(result, AssetAny) and len(result.assets) == 3

    def test_assetany_repr(self):
        assert "AssetAny(" in repr(a | b)

    def test_collection_names_include_assetref(self):
        # AssetAll built from a ref + an asset; the name collection must handle both.
        coll = a.within(hours=24) & b
        names = coll.asset_names
        assert "ns/a" in names and "ns/b" in names

    def test_assetany_names_include_assetref(self):
        # AssetAny holding an AssetRef exercises the AssetRef arm of asset_names.
        names = (a.within(hours=24) | b).asset_names
        assert "ns/a" in names and "ns/b" in names

    def test_asset_to_dict_includes_extra(self):
        asset = Asset("ns/withextra", extra={"note": "x"})
        assert asset.to_dict().get("extra") == {"note": "x"}


# ============================================================ #
# AssetAlias operators
# ============================================================ #
class TestAssetAlias:
    def _alias(self, name="grp"):
        return AssetAlias(name=name, assets=[a, b])

    def test_equality(self):
        assert self._alias() == self._alias()
        assert self._alias() != self._alias("other")
        assert self._alias() != 123

    def test_asset_names(self):
        assert self._alias().asset_names == ["ns/a", "ns/b"]

    def test_and_with_alias(self):
        assert isinstance(self._alias() & self._alias("g2"), AssetAll)

    def test_and_with_assetall(self):
        assert isinstance(self._alias() & (a & c), AssetAll)

    def test_and_with_asset(self):
        assert isinstance(self._alias() & c, AssetAll)

    def test_and_bad_type_raises(self):
        with pytest.raises(TypeError):
            self._alias() & 42

    def test_or_with_alias(self):
        assert isinstance(self._alias() | self._alias("g2"), AssetAny)

    def test_or_with_assetany(self):
        assert isinstance(self._alias() | (a | c), AssetAny)

    def test_or_with_asset(self):
        assert isinstance(self._alias() | c, AssetAny)

    def test_or_bad_type_raises(self):
        with pytest.raises(TypeError):
            self._alias() | 42


# ============================================================ #
# normalize_asset_schedule — list forms
# ============================================================ #
class TestNormalizeSchedule:
    def test_single_item_alias(self):
        result = normalize_asset_schedule([AssetAlias(name="g", assets=[a, b])])
        assert isinstance(result, AssetAny)

    def test_single_item_asset(self):
        assert isinstance(normalize_asset_schedule([a]), AssetAll)

    def test_single_item_assetall_passthrough(self):
        all_ab = a & b
        assert normalize_asset_schedule([all_ab]) is all_ab

    def test_multiple_items_plain(self):
        result = normalize_asset_schedule([a, b])
        assert isinstance(result, AssetAll) and len(result.assets) == 2

    def test_multiple_items_with_operator_returns_as_is(self):
        result = normalize_asset_schedule([a, b & c])
        assert isinstance(result, AssetAll)

    def test_alias_in_multiple_items_expands(self):
        result = normalize_asset_schedule([a, AssetAlias(name="g", assets=[b, c])])
        assert isinstance(result, AssetAll)

    def test_unrecognized_schedule_returns_none(self):
        # A cron string is not an asset schedule → falls through to None.
        assert normalize_asset_schedule("rate(1 hour)") is None


class TestAssetAllOrFlattensAssetAny:
    """(a & b) | (c | d): OR is associative — the right-hand AssetAny must be
    flattened into the result, not nested. A nested AssetAny is invisible to
    asset_names/to_dict serialization, silently dropping trigger operands."""

    def test_or_with_assetany_flattens(self):
        a = Asset(name="asset_a")
        b = Asset(name="asset_b")
        c = Asset(name="asset_c")
        d = Asset(name="asset_d")
        combined = (a & b) | (c | d)
        assert isinstance(combined, AssetAny)
        # three operands: the AssetAll plus the two flattened assets
        assert len(combined.assets) == 3
        assert combined.assets[1] is c and combined.assets[2] is d
        # serialization sees every operand
        names = combined.asset_names
        assert any("asset_c" in n for n in names)
        assert any("asset_d" in n for n in names)
        as_dict = combined.to_dict()
        assert as_dict["operator"] == "OR"
        assert len(as_dict["assets"]) == 3


def test_wait_for_metadata_includes_groups():
    """Regression: the metadata/lineage serializer must include AssetAll/AssetAny
    groups, matching the runtime serializer. It used to silently drop them, so a
    task waiting on `AssetAll([a, b])` showed no dependency at all in lineage."""
    from polyris.assets import Asset, AssetAll, AssetAny
    from polyris.generators import _serialize_wait_for, _serialize_wait_for_metadata

    a = Asset(name="grp_a")
    b = Asset(name="grp_b")

    for combiner, op in ((AssetAll, "AND"), (AssetAny, "OR")):
        wf = [combiner([a, b])]
        meta = _serialize_wait_for_metadata(wf)
        runtime = _serialize_wait_for(wf)
        # metadata is no longer empty and carries the operator + both assets
        assert meta and meta[0]["operator"] == op
        assert {x["name"] for x in meta[0]["assets"]} == {"grp_a", "grp_b"}
        # and it agrees with the runtime serializer on the operator
        assert runtime[0]["operator"] == op
