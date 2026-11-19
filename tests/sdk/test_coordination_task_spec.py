"""Spec texts, their hashes and a version's fingerprint (ADR #137 §8, §9)."""


import pytest

from polyris.constants import TaskSpecKey
from polyris.coordination import run_row, task_spec

SPEC = {"task_type": "glue", "retries": 2, "timeout_hours": 1.0, "description": "Ladung für Kunden"}


def test_a_spec_has_one_text_sorted_compact_with_its_characters_as_they_are():
    text = task_spec.canonical_json(SPEC)
    assert text == '{"description":"Ladung für Kunden","retries":2,"task_type":"glue","timeout_hours":1.0}'
    assert task_spec.canonical_json(dict(reversed(list(SPEC.items())))) == text


def test_a_spec_without_a_json_number_is_refused():
    with pytest.raises(ValueError):
        task_spec.canonical_json({"limit": float("nan")})


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


# ── reading a spec back (§8: Start_Run checks what it reads) ─────────


LOAD = {"task_id": "load", "task_type": "glue", "task_arn": "arn:aws:glue:us-east-1:123456789012:job/load",
        "task_config": {"job_name": "load", "retries": 2}, "dependencies": ["extract"],
        "trigger_rule": "all_success", "orchestration_timeout": 3600, "wait_before": 0,
        "wait_for": [{"asset_name": "s3/orders", "freshness_hours": 24}], "outlets": [{"name": "s3/sales"}]}


def stored(spec):
    text = task_spec.canonical_json(spec)
    return text, task_spec.text_hash(text)


def parsed(spec):
    text, digest = stored(spec)
    return task_spec.parse(text, expected_hash=digest)


def test_a_spec_reads_back_with_the_fields_the_dispatcher_decides_on():
    spec = parsed(LOAD)
    assert spec.task_id == "load" and spec.text_hash == stored(LOAD)[1]
    assert spec.decision_fields() == {"dependencies": ["extract"], "trigger_rule": "all_success",
                                      "orchestration_timeout": 3600, "wait_before": 0,
                                      "wait_for": [{"asset_name": "s3/orders", "freshness_hours": 24}]}


def test_a_spec_without_its_optional_keys_carries_only_the_required_decisions():
    spec = parsed({"task_id": "extract", "task_type": "sfn", "dependencies": [], "trigger_rule": "all_success"})
    assert spec.decision_fields() == {"dependencies": [], "trigger_rule": "all_success"}


def test_a_text_that_does_not_match_its_hash_is_refused():
    text, _ = stored(LOAD)
    with pytest.raises(task_spec.SpecError, match="does not match its hash"):
        task_spec.parse(text, expected_hash="0" * 64)


@pytest.mark.parametrize("text,problem", [("{", "is not JSON"), ("[]", "must be a JSON object")])
def test_a_text_that_is_not_a_json_object_is_refused(text, problem):
    with pytest.raises(task_spec.SpecError, match=problem):
        task_spec.parse(text, expected_hash=task_spec.text_hash(text))


@pytest.mark.parametrize("change,problem", [
    ({"retries": 2}, r"unknown keys \['retries'\]"),                  # retries live in task_config
    ({"trigger_rule": None}, "trigger_rule must be a non-empty string"),
    ({"task_id": " "}, "task_id must be a non-empty string"),
    ({"task_type": 7}, "task_type must be a non-empty string"),
    ({"dependencies": "extract"}, "dependencies must be a list"),
    ({"dependencies": ["extract", ""]}, "dependencies must be a list"),
    ({"dependencies": ["extract", "extract"]}, "name a task twice"),
    ({"execution_timeout": 0}, "execution_timeout must be a whole number of seconds, at least 1"),
    ({"orchestration_timeout": True}, "orchestration_timeout must be a whole number"),
    ({"orchestration_timeout": 1.5}, "orchestration_timeout must be a whole number"),
    ({"wait_before": -1}, "wait_before must be a whole number of seconds, at least 0"),
    ({"skip_on_backfill": "yes"}, "skip_on_backfill must be true or false"),
    ({"wait_for": [{"name": "s3/orders"}]}, "wait_for: "),
])
def test_a_spec_that_cannot_be_run_is_refused_with_its_cause(change, problem):
    with pytest.raises(task_spec.SpecError, match=problem):
        parsed({**LOAD, **change})


def test_a_spec_without_a_required_key_is_refused_with_the_names():
    spec = {k: v for k, v in LOAD.items() if k not in ("task_type", "trigger_rule")}
    with pytest.raises(task_spec.SpecError, match=r"spec of 'load': missing keys \['task_type', 'trigger_rule'\]"):
        parsed(spec)


def test_a_rule_this_platform_does_not_know_is_left_to_the_dispatcher():
    """ADR #138 §2: it fails its task at run time, not Start_Run: validation refuses it before a deploy."""
    assert parsed({**LOAD, "trigger_rule": "none_failed_or_skipped"}).decision_fields()["trigger_rule"] == \
        "none_failed_or_skipped"


def test_the_decision_fields_survive_dynamodb_as_text():
    """As attributes, boto3's resource gives numbers back as Decimal, which the asset parser refuses."""
    from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

    spec = parsed({**LOAD, "wait_for": [{"asset_name": "s3/orders", "freshness_hours": 1.5}]})
    stored = TypeSerializer().serialize(task_spec.decision_text(spec))
    fields = task_spec.decision_from_text(TypeDeserializer().deserialize(stored))
    assert fields == spec.decision_fields()
    assert fields["wait_for"][0]["freshness_hours"] == 1.5 and isinstance(fields["orchestration_timeout"], int)


@pytest.mark.parametrize("text,problem", [
    ("{", "not JSON"), ("[]", "must be a JSON object"), ('{"dependencies": []}', r"missing \['trigger_rule'\]"),
    ('{"dependencies": [], "trigger_rule": "all_success", "task_arn": "x"}', r"unknown \['task_arn'\]"),
    ('{"dependencies": "x", "trigger_rule": "all_success"}', "dependencies must be a list"),
])
def test_decision_fields_of_another_shape_are_refused(text, problem):
    with pytest.raises(task_spec.SpecError, match=problem):
        task_spec.decision_from_text(text)


def test_the_parser_knows_exactly_the_spec_keys():
    assert {k.value for k in task_spec.REQUIRED} | {k.value for k in task_spec.DECISION_KEYS} <= {
        k.value for k in TaskSpecKey}
    assert parsed({k.value: LOAD.get(k.value, False) for k in TaskSpecKey if k.value in LOAD}).task_id == "load"
