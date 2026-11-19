"""Writing and reading ``pipeline-specs`` (ADR #137 §8), on moto at the boto3 boundary."""

import pytest
from botocore.exceptions import ClientError

from polyris.coordination import spec_store, task_spec
from polyris.coordination.task_spec import PK, SK, TEXT, VERSION_HASH

TABLE = "acme-dev-polyris-pipeline-specs"  # conftest.SPECS_TABLE, which platform_dynamodb creates


def _spec(task_id, **extra):
    return {"task_id": task_id, "task_type": "sfn", "dependencies": [], "trigger_rule": "all_success", **extra}


def _version(*specs, pipeline="orders"):
    return task_spec.build_version(pipeline, specs)


def _items(client):
    return client.scan(TableName=TABLE)["Items"]


class _Recording:
    """Forwards to the real (moto) client and records each call's name; ``adjust`` may change a call."""

    def __init__(self, client, adjust=None):
        self.client, self.calls, self.adjust = client, [], adjust or {}

    def __getattr__(self, name):
        method = getattr(self.client, name)

        def call(**kwargs):
            self.calls.append(name)
            if name in self.adjust:
                return self.adjust[name](method, kwargs)
            return method(**kwargs)
        return call


def test_a_version_is_written_and_read_back_whole(platform_dynamodb):
    version = _version(_spec("extract"), _spec("load", dependencies=["extract"]))
    assert spec_store.write(platform_dynamodb, TABLE, version) == 2
    specs = spec_store.read(platform_dynamodb, TABLE, version.spec_id, expected_count=2)
    assert {k: v.fields for k, v in specs.items()} == {
        "extract": _spec("extract"), "load": _spec("load", dependencies=["extract"])}
    spec_store.verify(platform_dynamodb, TABLE, version)


def test_a_text_is_stored_once_and_a_version_only_names_it(platform_dynamodb):
    """D95: two tasks or two deploys with the same text share one text item."""
    first = _version(_spec("extract"), _spec("load"))
    assert spec_store.write(platform_dynamodb, TABLE, first) == 2
    second = _version(_spec("extract"), _spec("load", wait_before=30))
    recording = _Recording(platform_dynamodb)
    assert spec_store.write(recording, TABLE, second) == 1
    assert recording.calls.count("put_item") == 1
    texts = [i for i in _items(platform_dynamodb) if i[PK]["S"] == "orders#text"]
    versions = [i for i in _items(platform_dynamodb) if i[PK]["S"] != "orders#text"]
    assert len(texts) == 3 and len(versions) == 4
    assert all(set(i) == {PK, SK, VERSION_HASH} for i in versions)
    assert all(set(i) == {PK, SK, TEXT} for i in texts)


def test_a_text_written_by_another_deploy_meanwhile_counts_as_written(platform_dynamodb):
    version = _version(_spec("extract"))

    def race(method, kwargs):
        method(**{k: v for k, v in kwargs.items() if k not in ("ConditionExpression", "ExpressionAttributeNames")})
        return method(**kwargs)  # the conditional put now finds the text there

    assert spec_store.write(_Recording(platform_dynamodb, {"put_item": race}), TABLE, version) == 0
    spec_store.verify(platform_dynamodb, TABLE, version)


def test_any_other_put_error_reaches_the_caller(platform_dynamodb):
    def denied(method, kwargs):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "PutItem")

    with pytest.raises(ClientError, match="AccessDeniedException"):
        spec_store.write(_Recording(platform_dynamodb, {"put_item": denied}), TABLE, _version(_spec("extract")))


def test_unprocessed_items_are_sent_again(platform_dynamodb):
    version = _version(*[_spec(f"t{i}") for i in range(30)])
    left = {"once": True}

    def drop_one(method, kwargs):
        request = kwargs["RequestItems"][TABLE]
        if left.pop("once", False):
            method(RequestItems={TABLE: request[1:]})
            return {"UnprocessedItems": {TABLE: request[:1]}}
        return method(**kwargs)

    sleeps = []
    spec_store.write(_Recording(platform_dynamodb, {"batch_write_item": drop_one}), TABLE, version,
                     sleep=sleeps.append)
    assert sleeps == [spec_store.BACKOFF_SECONDS]
    spec_store.verify(platform_dynamodb, TABLE, version)


def test_items_left_unprocessed_through_every_attempt_stop_the_write(platform_dynamodb):
    def never(method, kwargs):
        return {"UnprocessedItems": kwargs["RequestItems"]}

    with pytest.raises(spec_store.Unprocessed, match="left 1 item"):
        spec_store.write(_Recording(platform_dynamodb, {"batch_write_item": never}), TABLE,
                         _version(_spec("extract")), sleep=lambda _: None)


def test_unprocessed_keys_are_read_again_and_a_read_that_never_ends_stops(platform_dynamodb):
    version = _version(_spec("extract"), _spec("load"))
    spec_store.write(platform_dynamodb, TABLE, version)
    left = {"once": True}

    def keep_one(method, kwargs):
        response = method(**kwargs)
        if left.pop("once", False):
            request = kwargs["RequestItems"][TABLE]
            response["Responses"][TABLE] = response["Responses"][TABLE][:-1]
            response["UnprocessedKeys"] = {TABLE: {**request, "Keys": request["Keys"][-1:]}}
        return response

    recording = _Recording(platform_dynamodb, {"batch_get_item": keep_one})
    assert len(spec_store.read(recording, TABLE, version.spec_id, expected_count=2, sleep=lambda _: None)) == 2

    def never(method, kwargs):
        return {"Responses": {TABLE: []}, "UnprocessedKeys": kwargs["RequestItems"]}

    with pytest.raises(spec_store.Unprocessed, match="unread"):
        spec_store.read(_Recording(platform_dynamodb, {"batch_get_item": never}), TABLE, version.spec_id,
                        expected_count=2, sleep=lambda _: None)


def test_a_version_is_read_page_by_page(platform_dynamodb):
    version = _version(*[_spec(f"t{i}") for i in range(5)])
    spec_store.write(platform_dynamodb, TABLE, version)
    paged = _Recording(platform_dynamodb, {"query": lambda method, kwargs: method(**kwargs, Limit=2)})
    assert len(spec_store.read(paged, TABLE, version.spec_id, expected_count=5)) == 5
    assert paged.calls.count("query") == 3


@pytest.mark.parametrize("fault,problem", [
    ("drop a version item", "has 1 task"),
    ("alter a version item", "does not match its fingerprint"),
    ("drop a text", "is missing"),
    ("alter a text", "does not match its hash"),
])
def test_a_version_that_is_not_what_was_written_is_refused(platform_dynamodb, fault, problem):
    version = _version(_spec("extract"), _spec("load"))
    spec_store.write(platform_dynamodb, TABLE, version)
    text_hash = version.hashes["load"]
    if fault == "drop a version item":
        platform_dynamodb.delete_item(TableName=TABLE, Key={PK: {"S": version.spec_id}, SK: {"S": "load"}})
    elif fault == "alter a version item":
        platform_dynamodb.put_item(TableName=TABLE, Item={PK: {"S": version.spec_id}, SK: {"S": "load"},
                                                          VERSION_HASH: {"S": version.hashes["extract"]}})
    elif fault == "drop a text":
        platform_dynamodb.delete_item(TableName=TABLE, Key={PK: {"S": "orders#text"}, SK: {"S": text_hash}})
    else:
        platform_dynamodb.put_item(TableName=TABLE, Item={PK: {"S": "orders#text"}, SK: {"S": text_hash},
                                                          TEXT: {"S": version.texts["load"].replace("sfn", "glue")}})
    with pytest.raises(task_spec.SpecError, match=problem):
        spec_store.verify(platform_dynamodb, TABLE, version)


def test_a_version_of_no_tasks_reads_back_empty(platform_dynamodb):
    version = _version()
    spec_store.write(platform_dynamodb, TABLE, version)
    assert spec_store.read(platform_dynamodb, TABLE, version.spec_id, expected_count=0) == {}
