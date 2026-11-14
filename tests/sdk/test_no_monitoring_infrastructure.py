"""The platform template ships no custom metrics, alarms, SNS topics or DLQs (CLAUDE.md Principle #45).

Failures heal in code, fail the affected task loudly, or show in the console
as a warning or the engine-health banner (ADR #130 §9). Monitoring
infrastructure costs every user money and needs someone to watch it; adding
any needs an explicit maintainer decision recorded in an ADR.
"""
import json
from pathlib import Path

import pytest
import yaml

TEMPLATE = Path(__file__).resolve().parents[2] / "sam" / "template.yaml"

FORBIDDEN_TYPES = {
    "AWS::CloudWatch::Alarm", "AWS::CloudWatch::CompositeAlarm", "AWS::CloudWatch::AnomalyDetector",
    "AWS::CloudWatch::Dashboard", "AWS::Logs::MetricFilter", "AWS::SNS::Topic", "AWS::SNS::Subscription",
    "AWS::SNS::TopicPolicy", "AWS::SQS::Queue",
}
FORBIDDEN_PROPERTIES = {"DeadLetterConfig", "DeadLetterQueue", "DestinationConfig", "OnFailure"}
FORBIDDEN_ACTIONS = {"cloudwatch:PutMetricData", "cloudwatch:PutMetricAlarm", "sns:Publish", "sqs:SendMessage",
                     "cloudwatch:*", "sns:*", "sqs:*"}


@pytest.fixture(scope="module")
def resources():
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor("!", lambda loader, suffix, node: (
        loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode)
        else loader.construct_sequence(node, deep=True) if isinstance(node, yaml.SequenceNode)
        else loader.construct_mapping(node, deep=True)))
    return yaml.load(TEMPLATE.read_text(), Loader=Loader)["Resources"]


def _keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _keys(inner)


def test_no_alarm_topic_queue_or_dashboard_resources(resources):
    assert {name: r["Type"] for name, r in resources.items() if r["Type"] in FORBIDDEN_TYPES} == {}


def test_no_dead_letter_queues_or_failure_destinations(resources):
    found = {name: sorted(set(_keys(r.get("Properties", {}))) & FORBIDDEN_PROPERTIES) for name, r in resources.items()}
    assert {name: keys for name, keys in found.items() if keys} == {}


def test_no_role_may_publish_metrics_alarms_or_messages(resources):
    text = json.dumps(resources)
    assert [action for action in sorted(FORBIDDEN_ACTIONS) if f'"{action}"' in text] == []
