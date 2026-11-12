"""AssetEventsRepo.facts — the console's FactReader over the partition index (ADR #130 §4).

Runs against moto tables built from sam/template.yaml, so the index name, its
keys and its projection are the deployed ones (Principle #13).
"""
import importlib.util
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from coordination_core.asset_conditions import evaluate
from coordination_core.spec import parse_schedule

_KIT_PATH = Path(__file__).resolve().parents[3] / "coordination" / "tests" / "coordination_testkit.py"
_spec = importlib.util.spec_from_file_location("coordination_testkit", _KIT_PATH)
assert _spec and _spec.loader
kit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kit)

NOW = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)
D = date(2026, 11, 12)


@pytest.fixture
def repo(mocker):
    from dal.assets_repo import AssetEventsRepo

    logical, name = kit.TABLES["ASSET_EVENTS_TABLE"]
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        kit.create_tables(resource, {logical: name})
        mocker.patch.object(AssetEventsRepo, "table", new_callable=mocker.PropertyMock,
                            return_value=resource.Table(name))
        yield AssetEventsRepo()


def _fact(repo, asset: str, key: str, *, granularity=None, minutes: int = 0):
    return repo.write_fact(asset_name=asset, partition_key=key, granularity=granularity, cascade="auto",
                           source_dag="producer", source_task="load", uri="", ttl_days=30,
                           now=NOW + timedelta(minutes=minutes))


def test_reads_the_range_newest_partition_first(repo):
    for day in ("2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"):
        _fact(repo, "sales", day)
    _fact(repo, "prices", "2026-11-11")

    found = [f.partition_date.isoformat() for f in repo.facts("sales", last=D, first=date(2026, 11, 10))]

    assert found == ["2026-11-12", "2026-11-11", "2026-11-10"]


def test_without_a_lower_bound_it_reads_everything_up_to_last(repo):
    _fact(repo, "sales", "2026-10-01")
    _fact(repo, "sales", "2026-11-12")
    _fact(repo, "sales", "2026-11-13")

    assert [f.partition_date for f in repo.facts("sales", last=D, first=None)] == [D, date(2026, 10, 1)]


def test_a_fact_keeps_its_granularity(repo):
    _fact(repo, "prices", "2026-W46", granularity="weekly")

    (fact,) = repo.facts("prices", last=D, first=None)

    assert (fact.partition_date, fact.granularity) == (date(2026, 11, 9), "weekly")


def test_rows_without_partition_fields_are_not_read(repo):
    repo.put({"asset_name": "sales", "event_time": "2026-11-12T05:00:00Z", "execution_date": "2026-11-12"})

    assert list(repo.facts("sales", last=D, first=None)) == []


def test_it_feeds_asset_conditions(repo):
    schedule = parse_schedule({"operator": "AND", "assets": ["sales", "prices"]})
    _fact(repo, "sales", "2026-11-12")

    waiting = evaluate(schedule, target=D, now=NOW, reader=repo)
    _fact(repo, "prices", "2026-W46", granularity="weekly", minutes=1)
    ready = evaluate(schedule, target=D, now=NOW, reader=repo)

    assert not waiting.ready
    assert {a.asset_name: a.ready for a in waiting.assets} == {"sales": True, "prices": False}
    assert ready.ready


def test_a_manual_fact_keeps_the_metadata_a_person_attached(repo):
    item = repo.write_fact(asset_name="sales", partition_key="2026-11-12", granularity=None, cascade="auto",
                           source_dag="console", source_task="manual", uri="", ttl_days=30,
                           metadata={"note": "re-run after the outage"}, now=NOW)

    stored = repo.table.get_item(Key={"asset_name": "sales", "event_time": item["event_time"]})["Item"]

    assert stored["metadata"] == {"note": "re-run after the outage"}
    assert stored["partition_date"] == "2026-11-12"


def test_it_reads_every_page(mocker):
    from dal.assets_repo import AssetEventsRepo

    def page(day, more):
        item = {"asset_name": "sales", "event_time": f"{day}T05:00:00.000Z", "partition_key": day,
                "partition_date": day, "granularity": "daily"}
        return {"Items": [item], **({"LastEvaluatedKey": {"asset_name": "sales", "event_time": day}} if more else {})}

    table = mocker.MagicMock()
    table.query.side_effect = [page("2026-11-12", True), page("2026-11-11", False)]
    mocker.patch.object(AssetEventsRepo, "table", new_callable=mocker.PropertyMock, return_value=table)

    found = [f.partition_date.isoformat() for f in AssetEventsRepo().facts("sales", last=D, first=None)]

    assert found == ["2026-11-12", "2026-11-11"]
    assert table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {"asset_name": "sales", "event_time": "2026-11-12"}
