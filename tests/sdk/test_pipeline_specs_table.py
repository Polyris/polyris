"""The ``pipeline-specs`` table in ``sam/template.yaml`` (ADR #137 §8, *Migration and upgrade*).

No TTL: versions and texts are never deleted, since a redrive runs the original
definition for up to 14 days and identical DAGs share their items. No deletion
protection: it would make ``polyris-down`` and the rollback of a failed first
install fail. No ``Retain``: a table a rollback left behind would block the next
``polyris-up``. Its keys are the ones ``task_spec`` writes and reads.
"""

from pathlib import Path


from polyris import deploy
from polyris.coordination import task_spec
from sam_template import load_template

REPO_ROOT = Path(__file__).resolve().parents[2]


def _template() -> dict:
    return load_template(tagged=True)


TEMPLATE = _template()
TABLE = TEMPLATE["Resources"]["PipelineSpecsTable"]


def test_the_table_has_no_ttl_no_deletion_protection_and_no_retain():
    props = TABLE["Properties"]
    assert "TimeToLiveSpecification" not in props
    assert "DeletionProtectionEnabled" not in props
    assert "DeletionPolicy" not in TABLE and "UpdateReplacePolicy" not in TABLE


def test_the_table_is_on_demand_with_string_keys_named_as_task_spec_writes_them():
    props = TABLE["Properties"]
    assert props["BillingMode"] == "PAY_PER_REQUEST"
    assert props["KeySchema"] == [{"AttributeName": task_spec.PK, "KeyType": "HASH"},
                                  {"AttributeName": task_spec.SK, "KeyType": "RANGE"}]
    assert props["AttributeDefinitions"] == [{"AttributeName": task_spec.PK, "AttributeType": "S"},
                                             {"AttributeName": task_spec.SK, "AttributeType": "S"}]
    assert props["TableName"] == {"!Sub": "${Namespace}-${Stage}-polyris-pipeline-specs"}


def test_polyris_deploy_reads_the_tables_name_from_an_output_of_the_stack():
    assert "PipelineSpecsTable" in deploy.SAM_STACK_OUTPUT_KEYS
    assert TEMPLATE["Outputs"]["PipelineSpecsTable"]["Value"] == {"!Ref": "PipelineSpecsTable"}


def test_the_console_reads_the_specs_and_writes_none():
    console = TEMPLATE["Resources"]["ConsoleApiFunction"]["Properties"]["Environment"]["Variables"]
    assert console["SPECS_TABLE"] == {"!Ref": "PipelineSpecsTable"}
    statements = TEMPLATE["Resources"]["ConsoleApiRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    on_specs = [s for s in statements if s.get("Resource") == {"!GetAtt": "PipelineSpecsTable.Arn"}]
    assert [sorted(s["Action"]) for s in on_specs] == [["dynamodb:BatchGetItem", "dynamodb:GetItem", "dynamodb:Query"]]
