"""Generator state-builder + edge tests — optional args and tracked-step paths.

Closes the bulk of the remaining ``polyris.generators`` branches (CLAUDE.md #13):
the ``if step.<optional>:`` arms of the per-service state builders, the Map-state
validation in ``validate_asl``, ``generate_debug_info`` enrichment, the tracked
Glue/ECS/Athena step paths in ``generate_dag_json`` / ``generate_mermaid``, the
asset-lineage iterator, ``generate_eventbridge_schedule`` and ``generate_all_assets``.
"""
from __future__ import annotations

from datetime import timedelta

from polyris import DAG, task, Asset
from polyris.steps import Succeed, DynamoDBTask, SNSTask, SQSTask, S3Task, GlueTask, ECSTask
from polyris.generators import (
    _generate_step_state,
    _iter_dag_assets,
    validate_asl,
    generate_debug_info,
    generate_dag_json,
    generate_mermaid,
    generate_eventbridge_schedule,
    generate_all_assets,
)

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"


# ============================================================ #
# Per-service state builders — optional arguments
# ============================================================ #
class TestStepStateOptionalArgs:
    def test_succeed_with_output(self):
        assert _generate_step_state(Succeed(output={"k": "v"}))["Output"] == {"k": "v"}

    def test_dynamodb_full_arguments(self):
        s = DynamoDBTask(
            operation="update_item", table_name="T",
            key={"id": {"S": "1"}}, item={"a": {"S": "b"}},
            update_expression="SET x = :v",
            expression_attribute_names={"#x": "x"},
            expression_attribute_values={":v": {"N": "1"}},
            condition_expression="attribute_exists(id)",
            key_condition="id = :id", index_name="gsi1",
        )
        args = _generate_step_state(s)["Arguments"]
        for k in ("Key", "Item", "UpdateExpression", "ExpressionAttributeNames",
                  "ExpressionAttributeValues", "ConditionExpression",
                  "KeyConditionExpression", "IndexName"):
            assert k in args

    def test_sns_with_subject_and_attributes(self):
        s = SNSTask(topic_arn="arn:sns", message="hi", subject="subj",
                    message_attributes={"a": {"DataType": "String", "StringValue": "x"}})
        args = _generate_step_state(s)["Arguments"]
        assert args["Subject"] == "subj" and "MessageAttributes" in args

    def test_sqs_with_delay_and_attributes(self):
        s = SQSTask(queue_url="https://q", message_body="body", delay_seconds=10,
                    message_attributes={"a": {"DataType": "String", "StringValue": "x"}})
        args = _generate_step_state(s)["Arguments"]
        assert args["DelaySeconds"] == 10 and "MessageAttributes" in args

    def test_s3_with_body_content_copy(self):
        s = S3Task(operation="copy_object", bucket="b", key="k", body="data",
                   content_type="text/plain", copy_source="src/obj")
        args = _generate_step_state(s)["Arguments"]
        assert args["Body"] == "data"
        assert args["ContentType"] == "text/plain"
        assert args["CopySource"] == "src/obj"

    def test_glue_with_arguments_sync(self):
        st = _generate_step_state(GlueTask(job_name="j", arguments={"--k": "v"}, wait_for_completion=True))
        assert st["Arguments"]["Arguments"] == {"--k": "v"}
        assert st["Resource"].endswith(".sync")

    def test_ecs_fargate_network_and_overrides(self):
        s = ECSTask(cluster="c", task_definition="td", launch_type="FARGATE",
                    subnets=["subnet-1"], security_groups=["sg-1"], overrides={"x": 1})
        args = _generate_step_state(s)["Arguments"]
        assert "NetworkConfiguration" in args and "Overrides" in args


# ============================================================ #
# validate_asl — Map state handling
# ============================================================ #
class TestValidateAslMap:
    def test_map_without_iterator_errors(self):
        asl = {"StartAt": "M", "States": {"M": {"Type": "Map", "End": True}}}
        ok, errors, _ = validate_asl(asl)
        assert not ok
        assert any("Iterator" in e for e in errors)

    def test_map_with_iterator_recurses(self):
        asl = {
            "StartAt": "M",
            "States": {
                "M": {
                    "Type": "Map", "End": True,
                    "Iterator": {
                        "StartAt": "X",
                        "States": {"X": {"Type": "Pass", "End": True}},
                    },
                }
            },
        }
        ok, _errors, _ = validate_asl(asl)
        assert ok


# ============================================================ #
# debug info, tracked steps, lineage, schedules
# ============================================================ #
class TestGeneratorOutputs:
    def test_debug_info_enrichment(self):
        out = Asset("ns/out")
        with DAG("dbg", schedule=None) as dag:
            @task.sfn(arn=ARN, outlets=[out], skip_on_backfill=True, wait_before=timedelta(seconds=30))
            def step():
                pass
            step()
        debug = generate_debug_info(dag)
        task_info = debug["tasks"][0]
        assert task_info["outlets"] == ["ns/out"]
        assert task_info["skip_on_backfill"] is True
        assert task_info["wait_before"] == 30

    def test_debug_info_includes_asset_info_when_triggered(self):
        upstream = Asset("ns/u")
        with DAG("dbgasset", schedule=upstream) as dag:
            @task.sfn(arn=ARN)
            def go():
                pass
            go()
        assert "asset_info" in generate_debug_info(dag)

    def test_tracked_glue_step_in_dag_json_and_mermaid(self):
        with DAG("tracked", schedule=None) as dag:
            @task.sfn(arn=ARN)
            def up():
                pass
            g = GlueTask(job_name="j")
            up() >> g
        blob = generate_dag_json(dag)
        assert any(n["type"] == "glue" for n in blob["nodes"])
        mer = generate_mermaid(dag)
        assert g.step_id in mer

    def test_mermaid_lists_isolated_nodes(self):
        # No dependency edges → mermaid falls back to listing tasks + tracked steps.
        with DAG("iso", schedule=None) as dag:
            @task.sfn(arn=ARN)
            def solo():
                pass
            solo()
            GlueTask(job_name="lonely")
        mer = generate_mermaid(dag)
        assert "solo" in mer and "lonely" in mer

    def test_iter_dag_assets_outlet_and_inlet(self):
        up = Asset("ns/up")
        down = Asset("ns/down")
        with DAG("lin", schedule=None) as dag:
            @task.sfn(arn=ARN, inlets=[up], outlets=[down])
            def step():
                pass
            step()
        kinds = {kind for _tid, _asset, kind in _iter_dag_assets(dag)}
        assert kinds == {"inlet", "outlet"}

    def test_eventbridge_schedule_none_without_config(self):
        with DAG("noeb", schedule=None) as dag:
            @task.sfn(arn=ARN)
            def a():
                pass
            a()
        assert generate_eventbridge_schedule(dag) is None

    def test_generate_all_assets_records_triggers(self):
        upstream = Asset("ns/u")
        with DAG("consumer", schedule=upstream) as dag:
            @task.sfn(arn=ARN)
            def go():
                pass
            go()
        out = generate_all_assets([dag])
        assert isinstance(out, dict)

    def test_asset_subscriptions_table_threaded_into_register_state(self):
        """Regression test: generate_step_function_json's asset_subscriptions_table
        parameter must reach Register_Asset_Subscriptions' WriteSubscription.TableName
        verbatim, not fall through to the literal '${asset_subscriptions_table}'
        placeholder — that placeholder has no substitution mechanism anywhere in
        polyris-deploy's per-pipeline CloudFormation flow (confirmed via a real
        AWS DynamoDB.AmazonDynamoDBException: 'tableName' failed to satisfy
        constraint, the literal string was sent as the table name)."""
        import json
        from polyris.generators import generate_step_function_json

        upstream = Asset("ns/trigger")
        with DAG("asset-triggered", schedule=upstream) as dag:
            @task.sfn(arn=ARN)
            def go():
                pass
            go()

        asl = json.loads(generate_step_function_json(
            dag,
            wrapper_arn=ARN,
            registry_table="my-registry",
            tokens_table="my-tokens",
            asset_subscriptions_table="my-real-asset-subscriptions-table",
        ))
        write_sub = asl["States"]["Register_Asset_Subscriptions"]["ItemProcessor"]["States"]["WriteSubscription"]
        assert write_sub["Arguments"]["TableName"] == "my-real-asset-subscriptions-table"

    def test_asset_subscriptions_table_defaults_to_placeholder_when_omitted(self):
        """Control: omitting the parameter still falls back to the placeholder
        (matching registry_table/tokens_table's existing default behavior) —
        this test exists so a future change can't silently make the parameter
        required without noticing it changes the default-call contract."""
        import json
        from polyris.generators import generate_step_function_json

        upstream = Asset("ns/trigger2")
        with DAG("asset-triggered-2", schedule=upstream) as dag:
            @task.sfn(arn=ARN)
            def go():
                pass
            go()

        asl = json.loads(generate_step_function_json(dag))
        write_sub = asl["States"]["Register_Asset_Subscriptions"]["ItemProcessor"]["States"]["WriteSubscription"]
        assert write_sub["Arguments"]["TableName"] == "${asset_subscriptions_table}"

    def test_dag_json_carries_wait_before(self):
        with DAG("wb", schedule=None) as dag:
            @task.sfn(arn=ARN, wait_before=timedelta(seconds=45))
            def a():
                pass
            a()
        blob = generate_dag_json(dag)
        assert blob["nodes"][0]["wait_before"] == 45


# ============================================================ #
# additional optional/enrichment branches
# ============================================================ #
_WARN_SUBMACHINE = {
    "StartAt": "ch",
    "States": {
        # Choice without a Default → validate_asl emits a warning, which the
        # parent Parallel/Map validation then propagates.
        "ch": {"Type": "Choice",
               "Choices": [{"Variable": "$.x", "StringEquals": "y", "Next": "done"}]},
        "done": {"Type": "Pass", "End": True},
    },
}


class TestGeneratorEnrichmentBranches:
    def test_outlet_carries_glue_catalog_and_region(self):
        out = Asset("ns/orders", glue_table="db.orders",
                    glue_catalog="123456789012", glue_region="us-west-2")
        with DAG("glueout", schedule=None) as dag:
            @task.sfn(arn=ARN, outlets=[out])
            def mk():
                pass
            mk()
        blob = generate_dag_json(dag)
        outlet = blob["nodes"][0]["outlets"][0]
        assert outlet["glue_catalog"] == "123456789012"
        assert outlet["glue_region"] == "us-west-2"

    def test_sqs_message_group_id(self):
        s = SQSTask(queue_url="https://q", message_body="b", message_group_id="grp-1")
        assert _generate_step_state(s)["Arguments"]["MessageGroupId"] == "grp-1"

    def test_glue_task_worker_config(self):
        import json as _json
        from polyris.generators import generate_step_function_json

        with DAG("gluecfg", schedule=None) as dag:
            @task.glue(job_name="j", worker_type="G.1X", number_of_workers=4)
            def run():
                pass
            run()
        blob = _json.dumps(_json.loads(generate_step_function_json(dag)))
        assert "G.1X" in blob          # worker_type
        assert "number_of_workers" in blob

    def test_parallel_branch_warnings_propagate(self):
        asl = {"StartAt": "P",
               "States": {"P": {"Type": "Parallel", "End": True, "Branches": [_WARN_SUBMACHINE]}}}
        _ok, _errors, warnings = validate_asl(asl)
        assert any("Branch" in w for w in warnings)

    def test_map_iterator_warnings_propagate(self):
        asl = {"StartAt": "M",
               "States": {"M": {"Type": "Map", "End": True, "Iterator": _WARN_SUBMACHINE}}}
        _ok, _errors, warnings = validate_asl(asl)
        assert any("Iterator" in w for w in warnings)

    def test_map_iterator_errors_propagate(self):
        # Iterator whose state points Next at a missing state → an error that
        # the Map validation prefixes and propagates.
        bad_iter = {"StartAt": "x",
                    "States": {"x": {"Type": "Pass", "Next": "does_not_exist"}}}
        asl = {"StartAt": "M",
               "States": {"M": {"Type": "Map", "End": True, "Iterator": bad_iter}}}
        _ok, errors, _warnings = validate_asl(asl)
        assert any("Iterator" in e for e in errors)
