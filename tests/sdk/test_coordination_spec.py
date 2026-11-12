"""Asset condition specs, parsed from what the generator really writes."""

import pytest

from polyris.assets import Asset
from polyris.coordination.spec import AND, OR, AssetGroup, AssetLeaf, asset_names, parse
from polyris.generators import _serialize_wait_for

a, b, c = Asset("ns/a"), Asset("ns/b"), Asset("ns/c")


class TestParsesGeneratorOutput:
    def test_plain_within_and_consecutive(self):
        spec = parse(_serialize_wait_for([a, b.within(hours=6), c.consecutive(days=7)]))
        assert spec == AssetGroup(AND, (
            AssetLeaf("ns/a"),
            AssetLeaf("ns/b", freshness_hours=6),
            AssetLeaf("ns/c", consecutive_days=7),
        ))

    def test_nested_and_or(self):
        spec = parse(_serialize_wait_for([a & (b | c)]))
        assert spec == AssetGroup(AND, (
            AssetGroup(AND, (AssetLeaf("ns/a"), AssetGroup(OR, (AssetLeaf("ns/b"), AssetLeaf("ns/c"))))),
        ))
        assert asset_names(spec) == {"ns/a", "ns/b", "ns/c"}

    def test_empty_list_means_no_conditions(self):
        assert parse([]) == AssetGroup(AND, ())


class TestRejectsOtherShapes:
    @pytest.mark.parametrize("items", [
        {"asset_name": "a"},                                        # not a list
        ["a"],                                                      # item not an object
        [{"name": "a"}],                                            # metadata format, not SFN input
        [{"asset_name": "a", "name": "a"}],                         # unknown leaf key
        [{"asset_name": ""}],
        [{"asset_name": 7}],
        [{"asset_name": "a", "freshness_hours": 0}],
        [{"asset_name": "a", "freshness_hours": -1}],
        [{"asset_name": "a", "freshness_hours": True}],
        [{"asset_name": "a", "freshness_hours": "6"}],
        [{"asset_name": "a", "consecutive_days": 0}],
        [{"asset_name": "a", "consecutive_days": True}],
        [{"asset_name": "a", "consecutive_days": 2.5}],
        [{"asset_name": "a", "freshness_hours": 1, "consecutive_days": 2}],
        [{"operator": "XOR", "assets": [{"asset_name": "a"}]}],
        [{"operator": "OR", "assets": []}],
        [{"operator": "OR"}],
        [{"operator": "OR", "assets": [{"asset_name": "a"}], "extra": 1}],
    ])
    def test_invalid_specs_raise(self, items):
        with pytest.raises(ValueError):
            parse(items)
