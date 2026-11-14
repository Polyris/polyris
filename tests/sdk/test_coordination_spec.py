"""Asset condition specs, parsed from what the generator really writes."""

import pytest

from polyris.assets import Asset
from polyris.coordination.spec import AND, OR, AssetGroup, AssetLeaf, asset_names, parse, parse_schedule
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


class TestParsesTheRegistrySchedule:
    """parse_schedule reads DAG.asset_schedule_info, what the registry stores for push consumers."""

    @staticmethod
    def _registry_form(schedule):
        import warnings

        from polyris import DAG, task

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with DAG("pushed", schedule=schedule) as dag:
                @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:x")
                def report(): ...

                report()
        return dag.asset_schedule_info

    def test_and_of_names(self):
        a, b = Asset("s3://ns/a"), Asset("s3://ns/b")
        assert parse_schedule(self._registry_form([a, b])) == AssetGroup(AND, (AssetLeaf("ns/a"), AssetLeaf("ns/b")))

    def test_or_and_nested_groups_and_freshness(self):
        a, b, c = Asset("s3://ns/a"), Asset("s3://ns/b"), Asset("s3://ns/c")
        assert parse_schedule(self._registry_form([a.within(hours=2), b | c])) == AssetGroup(AND, (
            AssetLeaf("ns/a", freshness_hours=2), AssetGroup(OR, (AssetLeaf("ns/b"), AssetLeaf("ns/c")))))
        assert parse_schedule(self._registry_form([a | b])).operator == OR

    @pytest.mark.parametrize("bad", [[], "ns/a", {"asset_name": "ns/a"}, {"operator": "AND", "assets": []},
                                     {"operator": "AND", "assets": [{"name": "ns/a"}]}])
    def test_other_shapes_are_rejected(self, bad):
        with pytest.raises(ValueError):
            parse_schedule(bad)
