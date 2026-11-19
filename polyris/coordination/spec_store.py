"""Reading and writing ``pipeline-specs`` (ADR #137 §8).

``polyris-deploy`` writes a version with ``write`` and reads it back with
``read`` before it creates the change set; the console reads a pipeline's
version with ``read``, and so does ``Start_Run``. Each takes a low-level boto3
DynamoDB client, ``boto3.client("dynamodb")``: a resource's ``meta.client``
serializes the typed values these functions send a second time. The check of
what was read is the pure ``task_spec.check_version``.

- A text is written once: ``write`` reads which texts the table holds, and puts
  each missing one with a condition that counts a text already there as
  written. Versions and texts never change and are never deleted.
- Every read is strongly consistent, and every batch is retried until DynamoDB
  has processed all of it, within ``ATTEMPTS``. Any other error, an access
  denied included, reaches the caller unchanged.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Iterator, List, Mapping, Sequence, Tuple

from botocore.exceptions import ClientError

from . import task_spec
from .task_spec import PK, SK, TEXT, VERSION_HASH, SpecError, TaskSpec, Version

#: BatchWriteItem takes 25 items; BatchGetItem 100 keys.
WRITE_BATCH = 25
READ_BATCH = 100
#: How often a batch is sent again with what DynamoDB left unprocessed, backing off between tries.
ATTEMPTS = 8
BACKOFF_SECONDS = 0.05
BACKOFF_MAX_SECONDS = 2.0

Sleep = Callable[[float], None]


class Unprocessed(SpecError):
    """DynamoDB kept leaving part of a batch unprocessed through every attempt."""


def _chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _backoff(attempt: int) -> float:
    return float(min(BACKOFF_MAX_SECONDS, BACKOFF_SECONDS * (2 ** attempt)))


def _typed(item: Mapping[str, str]) -> Dict[str, Dict[str, str]]:
    return {name: {"S": value} for name, value in item.items()}


def _plain(item: Mapping[str, Mapping[str, str]]) -> Dict[str, str]:
    return {name: value["S"] for name, value in item.items() if "S" in value}


def _batch_get(client: Any, table: str, keys: List[Dict[str, Any]], *, projection: Sequence[str],
               sleep: Sleep) -> List[Dict[str, str]]:
    found: List[Dict[str, str]] = []
    names = {f"#a{i}": name for i, name in enumerate(projection)}
    for chunk in _chunks(keys, READ_BATCH):
        request: Dict[str, Any] = {table: {"Keys": list(chunk), "ConsistentRead": True,
                                           "ProjectionExpression": ", ".join(names),
                                           "ExpressionAttributeNames": names}}
        for attempt in range(ATTEMPTS):
            response = client.batch_get_item(RequestItems=request)
            found.extend(_plain(item) for item in response.get("Responses", {}).get(table, []))
            request = response.get("UnprocessedKeys") or {}
            if not request:
                break
            sleep(_backoff(attempt))
        else:
            raise Unprocessed(f"{table}: DynamoDB left {len(request[table]['Keys'])} key(s) unread "
                              f"after {ATTEMPTS} attempts")
    return found


def _batch_write(client: Any, table: str, items: List[Dict[str, str]], *, sleep: Sleep) -> None:
    for chunk in _chunks(items, WRITE_BATCH):
        request: Dict[str, Any] = {table: [{"PutRequest": {"Item": _typed(item)}} for item in chunk]}
        for attempt in range(ATTEMPTS):
            request = client.batch_write_item(RequestItems=request).get("UnprocessedItems") or {}
            if not request:
                break
            sleep(_backoff(attempt))
        else:
            raise Unprocessed(f"{table}: DynamoDB left {len(request[table])} item(s) unwritten "
                              f"after {ATTEMPTS} attempts")


def _held_texts(client: Any, table: str, version: Version, *, sleep: Sleep) -> set:
    keys = [{PK: {"S": item[PK]}, SK: {"S": item[SK]}} for item in version.text_items()]
    return {item[SK] for item in _batch_get(client, table, keys, projection=(SK,), sleep=sleep)}


def write(client: Any, table: str, version: Version, *, sleep: Sleep = time.sleep) -> int:
    """Write the texts the table does not hold yet, then every version item; the number of texts written."""
    held = _held_texts(client, table, version, sleep=sleep)
    written = 0
    for item in version.text_items():
        if item[SK] in held:
            continue
        try:
            client.put_item(TableName=table, Item=_typed(item), ConditionExpression="attribute_not_exists(#pk)",
                            ExpressionAttributeNames={"#pk": PK})
            written += 1
        except ClientError as exc:
            # Another deploy wrote the same text first; a text never changes, so it is written.
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
    _batch_write(client, table, version.version_items(), sleep=sleep)
    return written


def read_items(client: Any, table: str, version_id: str, *,
               sleep: Sleep = time.sleep) -> Tuple[List[Dict[str, str]], Dict[str, str]]:
    """A version's items, with a consistent paginated query, and their texts by hash."""
    items: List[Dict[str, str]] = []
    request: Dict[str, Any] = {"TableName": table, "ConsistentRead": True,
                               "KeyConditionExpression": "#pk = :pk",
                               "ExpressionAttributeNames": {"#pk": PK}, "ExpressionAttributeValues": {":pk": {"S": version_id}}}
    while True:
        response = client.query(**request)
        items.extend(_plain(item) for item in response.get("Items", []))
        if not response.get("LastEvaluatedKey"):
            break
        request["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    pipeline, _, _ = version_id.rpartition("#")
    partition = task_spec.text_partition(pipeline)
    digests = sorted({item[VERSION_HASH] for item in items if VERSION_HASH in item})
    keys = [{PK: {"S": partition}, SK: {"S": digest}} for digest in digests]
    texts = {item[SK]: item[TEXT] for item in _batch_get(client, table, keys, projection=(SK, TEXT), sleep=sleep)
             if TEXT in item}
    return items, texts


def read(client: Any, table: str, version_id: str, *, expected_count: int,
         sleep: Sleep = time.sleep) -> Dict[str, TaskSpec]:
    """Every spec of a version, checked against its count, each text's hash and the fingerprint."""
    items, texts = read_items(client, table, version_id, sleep=sleep)
    return task_spec.check_version(version_id, items, texts, expected_count=expected_count)


def verify(client: Any, table: str, version: Version, *, sleep: Sleep = time.sleep) -> None:
    """Read ``version`` back and refuse it unless the table holds exactly what was written.

    The fingerprint in ``spec_id`` is the sha256 of the version's (task_id, text hash)
    pairs, so a version that passes ``check_version`` with the written count holds
    exactly the texts written.
    """
    read(client, table, version.spec_id, expected_count=version.task_count, sleep=sleep)
