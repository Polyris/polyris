"""run_task's input, as the dispatcher builds it (ADR #137 §1, §9)."""

import json
from decimal import Decimal

import pytest

from polyris.coordination import retention, task_spec
from polyris.coordination import run_task_input as rti

LOAD = {"task_id": "load", "task_type": "glue", "task_arn": "arn:aws:glue:us-east-1:123456789012:job/load",
        "task_config": {"job_name": "load", "retries": 2}, "cross_account_role": "same",
        "dependencies": ["extract", "audit"], "trigger_rule": "all_done", "execution_timeout": 3600,
        "outlets": [{"name": "s3/sales"}]}


def spec(fields):
    text = task_spec.canonical_json(fields)
    return task_spec.parse(text, expected_hash=task_spec.text_hash(text))


def build(**overrides):
    arguments = dict(execution_name="load-2026-11-18-abcd1234", generation=3, task_name="load",
                     pipeline_name="orders", pipeline_execution="orders-run-2026-11-18-abcd1234",
                     date="2026-11-18", triggered_by="schedule", backfill_id=None, spec=spec(LOAD),
                     variables={"region": "eu"}, cascade="auto",
                     upstreams={"extract": ("failed", None), "audit": ("skipped", "manual")})
    arguments.update(overrides)
    return json.loads(rti.build(**arguments))


def test_the_input_is_the_canonical_text_that_was_measured_and_a_decimal_generation_is_a_number():
    text = rti.build(execution_name="load-x", generation=Decimal(3), task_name="load", pipeline_name="orders",
                     pipeline_execution="orders-run-x", date="2026-11-18", triggered_by="schedule",
                     backfill_id=None, spec=spec(LOAD), variables={"name": "Kyiv – Київ"}, cascade="auto",
                     upstreams={"extract": ("failed", None), "audit": ("skipped", "manual")})
    assert json.loads(text)["generation"] == 3
    assert "Київ" in text                                    # characters as they are, as the limit counts them


def test_the_input_carries_only_the_keys_run_task_reads():
    payload = build()
    assert set(payload) <= rti.KEYS
    assert set(payload) == rti.KEYS - {rti.Key.BACKFILL_ID}
    assert (payload["generation"], payload["task_type"], payload["execution_timeout"]) == (3, "glue", 3600)


def test_the_upstreams_arrive_with_their_statuses_and_skip_origins():
    upstreams = {"extract": ("failed", None), "audit": ("skipped", "manual")}
    assert build()["upstreams"] == {"audit": {"status": "skipped", "skip_origin": "manual"},
                                    "extract": {"status": "failed", "skip_origin": None}}
    assert json.loads(rti.upstream_statuses_env(upstreams)) == build()["upstreams"]


def test_the_lifetimes_come_from_retention_so_the_template_keeps_no_copy():
    payload = build()
    assert payload["output_ttl_seconds"] == retention.OUTPUT.total_seconds()
    assert payload["input_ttl_seconds"] == retention.INPUT.total_seconds()
    assert payload["history_ttl_seconds"] == retention.HISTORY.total_seconds()


def test_optional_fields_appear_only_when_there_is_something_to_say():
    plain = spec({"task_id": "load", "task_type": "sfn", "dependencies": [], "trigger_rule": "all_success"})
    payload = build(spec=plain, upstreams={}, backfill_id="bf-2026-11-18")
    assert payload["backfill_id"] == "bf-2026-11-18"
    assert not {"task_arn", "cross_account_role", "execution_timeout"} & set(payload)
    assert (payload["task_config"], payload["outlets"]) == ({}, [])


def test_a_pass_that_did_not_read_every_upstream_starts_nothing():
    with pytest.raises(ValueError, match="are not its dependencies"):
        build(upstreams={"extract": ("success", None)})


def test_a_spec_cannot_start_another_task():
    with pytest.raises(ValueError, match="cannot start 'report'"):
        build(task_name="report")


def test_an_input_over_256_kb_refuses_the_start_for_good():
    with pytest.raises(rti.InputTooLarge, match="over Step Functions' 262144"):
        build(variables={"blob": "x" * rti.PAYLOAD_LIMIT})
