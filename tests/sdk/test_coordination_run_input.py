"""The run's input item (ADR #137 §3, D96)."""

import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from polyris.constants import BackfillCascade
from polyris.coordination import records, run_input, run_row

RUN = "orders-run-2026-11-18-abcd1234"


def through_dynamodb(item):
    serializer, deserializer = TypeSerializer(), TypeDeserializer()
    return {k: deserializer.deserialize(serializer.serialize(v)) for k, v in item.items()}


def test_the_input_survives_dynamodb_with_its_numbers_as_they_were():
    original = run_input.RunInput({"threshold": 1.0, "regions": ["eu", "us"], "limit": 7}, ("audit",),
                                  BackfillCascade.NONE)
    item = run_input.to_item(original, pipeline_execution=RUN, ttl=1_830_000_000)
    assert item[run_row.TABLE_KEY] == run_row.input_key(RUN)
    back = run_input.from_item(through_dynamodb(item))
    assert back == original
    assert isinstance(back.variables["threshold"], float) and isinstance(back.variables["limit"], int)


def test_the_cascade_is_the_one_records_derives_from_the_starters_flags():
    starter = {"_suppress_asset_event": False, "cascade_all": True}
    item = run_input.to_item(run_input.RunInput(cascade=records.cascade_of(starter)), pipeline_execution=RUN, ttl=1)
    assert run_input.from_item(item).cascade == BackfillCascade.ALL


@pytest.mark.parametrize("change,problem", [
    ({"variables": {"a": 1}}, "must be JSON text"),
    ({"variables": "{"}, "is not JSON"),
    ({"variables": "[]"}, "must be a JSON object"),
    ({"skip_tasks": '"audit"'}, "JSON list of task names"),
    ({"skip_tasks": "[1]"}, "JSON list of task names"),
    ({"cascade": "sometimes"}, "cascade must be one of"),
    ({"partition_arg": 20261118}, "partition_arg must be text"),
])
def test_an_item_of_another_shape_is_refused(change, problem):
    item = {**run_input.to_item(run_input.RunInput(), pipeline_execution=RUN, ttl=1), **change}
    with pytest.raises(ValueError, match=problem):
        run_input.from_item(item)


def test_an_item_written_before_the_partition_arg_was_kept_reads_as_none():
    """The run's date stands in for it then (run_task_input.build)."""
    item = run_input.to_item(run_input.RunInput(), pipeline_execution=RUN, ttl=1)
    del item[run_input.PARTITION_ARG]
    assert run_input.from_item(item).partition_arg == ""
