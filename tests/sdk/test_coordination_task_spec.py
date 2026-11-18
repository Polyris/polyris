"""Spec texts, their hashes and a version's fingerprint (ADR #137 §8, §9)."""

import json

import pytest

from polyris.coordination import run_row, task_spec

SPEC = {"task_type": "glue", "retries": 2, "timeout_hours": 1.0, "description": "Ladung für Kunden"}


def test_a_spec_has_one_text_sorted_compact_with_its_characters_as_they_are():
    text = task_spec.canonical_json(SPEC)
    assert text == '{"description":"Ladung für Kunden","retries":2,"task_type":"glue","timeout_hours":1.0}'
    assert task_spec.canonical_json(dict(reversed(list(SPEC.items())))) == text


def test_a_spec_without_a_json_number_is_refused():
    with pytest.raises(ValueError):
        task_spec.canonical_json({"limit": float("nan")})


def test_the_text_keeps_1_0_where_dynamodb_would_give_back_1():
    """DynamoDB stores the number 1.0 as 1 (TR-0); moto keeps it, so only a live table shows this.
    A hash of the attributes read back would not match the deploy's; the stored text does."""
    text = task_spec.canonical_json(SPEC)
    read_back_as_attributes = {**SPEC, "timeout_hours": 1}
    assert task_spec.text_hash(task_spec.canonical_json(read_back_as_attributes)) != task_spec.text_hash(text)
    assert task_spec.canonical_json(json.loads(text)) == text


def test_a_texts_hash_is_the_sha256_of_its_utf8_bytes():
    assert task_spec.text_hash("{}") == "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    assert len(task_spec.text_hash(task_spec.canonical_json(SPEC))) == 64


def test_a_fingerprint_reads_its_pairs_in_key_order():
    pairs = [("load", "b" * 64), ("extract", "a" * 64)]
    assert task_spec.fingerprint(pairs) == task_spec.fingerprint(reversed(pairs))
    assert task_spec.fingerprint(pairs) != task_spec.fingerprint([("load", "c" * 64), ("extract", "a" * 64)])


def test_a_fingerprint_cannot_be_made_alike_by_moving_a_separator():
    assert task_spec.fingerprint([("a", "b#c")]) != task_spec.fingerprint([("a#b", "c")])


def test_a_version_names_each_task_once():
    with pytest.raises(ValueError, match=r"names a task twice: \['load'\]"):
        task_spec.fingerprint([("load", "a" * 64), ("load", "b" * 64)])


def test_the_keys_of_pipeline_specs():
    assert task_spec.spec_id("orders", "f" * 64) == "orders#" + "f" * 64
    assert task_spec.text_partition("orders") == "orders#text"


def test_the_format_is_one_number_that_the_registry_and_the_run_row_name_alike():
    assert isinstance(task_spec.FORMAT_VERSION, int) and task_spec.FORMAT_VERSION >= 1
    assert task_spec.REGISTRY_FORMAT_FIELD == run_row.FORMAT == "format_version"
