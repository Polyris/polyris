"""Step-state generation tests — the per-service ASL builders.

Constructs each ``Step`` subclass from ``polyris.steps`` and runs it through
``_generate_step_state`` (the dispatcher in ``polyris.generators``), asserting
the emitted ASL ``Resource`` and key ``Arguments``. This covers two modules at
once (CLAUDE.md #13): the Step dataclasses' construction/post-init and every
``_gen_<type>_state`` builder — including the service integrations that have no
public ``@task`` decorator (sns / sqs / s3 / dynamodb / eventbridge / bedrock /
http).
"""
from __future__ import annotations

import pytest

from polyris.steps import (
    Step,
    Wait,
    Pass,
    Succeed,
    Choice,
    Map,
    Sensor,
    ShortCircuit,
    LambdaTask,
    DynamoDBTask,
    SNSTask,
    SQSTask,
    S3Task,
    GlueTask,
    AthenaTask,
    ECSTask,
    EventBridgeTask,
    BedrockTask,
    HttpTask,
)
from polyris.generators import _generate_step_state


# ============================================================ #
# control-flow states
# ============================================================ #
class TestControlFlowStates:
    def test_wait_seconds(self):
        s = _generate_step_state(Wait(seconds=20))
        assert s == {"Type": "Wait", "Seconds": 20}

    def test_wait_timestamp(self):
        s = _generate_step_state(Wait(timestamp="2024-01-01T09:00:00Z"))
        assert s["Type"] == "Wait"
        assert s["Timestamp"] == "2024-01-01T09:00:00Z"

    def test_wait_timestamp_path(self):
        s = _generate_step_state(Wait(timestamp_path="$.scheduled_time"))
        assert s["TimestampPath"] == "$.scheduled_time"

    def test_wait_with_no_duration_raises(self):
        """Regression test: Wait() with none of seconds/timestamp/
        timestamp_path previously passed construction and local
        validate_asl_from_dag cleanly, generating {"Type": "Wait"} with no
        duration field — AWS Step Functions rejects a Wait state missing
        all of Seconds/Timestamp/SecondsPath/TimestampPath, so this only
        failed at actual deploy time, with a less helpful AWS-side error."""
        with pytest.raises(ValueError, match="requires exactly one of"):
            Wait()

    def test_pass_with_output(self):
        s = _generate_step_state(Pass(output={"k": "v"}))
        assert s["Type"] == "Pass"
        assert s["Output"] == {"k": "v"}

    def test_pass_bare(self):
        assert _generate_step_state(Pass()) == {"Type": "Pass"}

    def test_succeed(self):
        assert _generate_step_state(Succeed())["Type"] == "Succeed"

    def test_unknown_step_type_raises_clear_error(self):
        """An unrecognized step_type used to silently fall back to a no-op
        Pass state — meaning a pipeline with e.g. a mis-constructed step would
        deploy and 'succeed' while doing nothing for it, with zero warning
        anywhere (validate_asl/validate_asl_from_dag both saw a perfectly
        well-formed Pass state). It must now fail loudly instead."""
        with pytest.raises(ValueError, match="unrecognized step_type='mystery'"):
            _generate_step_state(Step(step_id="x", step_type="mystery"))

    def test_choice_map_sensor_short_circuit_are_not_yet_generatable(self):
        """Choice, Map, Sensor, and ShortCircuit are fully-implemented
        dataclasses (own step_type, docstrings with realistic usage examples,
        auto-register into the DAG exactly like every other Step subclass —
        see test_steps_core.py) but have no entry in _STEP_STATE_BUILDERS: no
        one ever wrote their `_gen_<type>_state` builder. Before the fix
        above, using any of them exactly as documented silently produced a
        no-op Pass state; now it fails loudly instead, at generation time —
        which is the correct interim behavior until (if) they're implemented,
        but the underlying feature gap is real and worth flagging: these are
        documented as usable, not marked experimental/unimplemented anywhere,
        unlike polyris.assets' explicit ExperimentalWarning."""
        for step in [
            Choice(step_id="c", default=Pass()),
            Map(items_path="$.rows"),
            Sensor(sensor_type="s3", bucket="b", key="k"),
            ShortCircuit(condition="{% true %}"),
        ]:
            with pytest.raises(ValueError, match=f"unrecognized step_type={step.step_type!r}"):
                _generate_step_state(step)


# ============================================================ #
# service-task states
# ============================================================ #
class TestServiceStates:
    def test_lambda(self):
        s = _generate_step_state(LambdaTask(function_arn="arn:aws:lambda:us-east-1:1:function:fn"))
        assert s["Resource"] == "arn:aws:states:::lambda:invoke"
        assert s["Arguments"]["FunctionName"].endswith(":fn")
        assert "Retry" not in s

    def test_lambda_with_retries_adds_retry(self):
        s = _generate_step_state(LambdaTask(function_arn="arn:fn", retries=3, retry_interval=5))
        assert s["Retry"][0]["MaxAttempts"] == 3

    def test_dynamodb_put(self):
        s = _generate_step_state(DynamoDBTask(operation="put_item", table_name="T", item={"id": {"S": "1"}}))
        assert s["Resource"] == "arn:aws:states:::dynamodb:putItem"
        assert s["Arguments"]["TableName"] == "T"
        assert s["Arguments"]["Item"] == {"id": {"S": "1"}}

    def test_dynamodb_query(self):
        s = _generate_step_state(DynamoDBTask(operation="query", table_name="T", key_condition="id = :v"))
        assert s["Resource"] == "arn:aws:states:::aws-sdk:dynamodb:query"
        assert s["Arguments"]["KeyConditionExpression"] == "id = :v"

    def test_sns(self):
        s = _generate_step_state(SNSTask(topic_arn="arn:topic", message="hi", subject="subj"))
        assert s["Resource"] == "arn:aws:states:::sns:publish"
        assert s["Arguments"]["TopicArn"] == "arn:topic"
        assert s["Arguments"]["Subject"] == "subj"

    def test_sqs(self):
        s = _generate_step_state(SQSTask(queue_url="https://q", message_body="body", delay_seconds=5))
        assert s["Resource"] == "arn:aws:states:::sqs:sendMessage"
        assert s["Arguments"]["QueueUrl"] == "https://q"
        assert s["Arguments"]["DelaySeconds"] == 5

    def test_s3_put(self):
        s = _generate_step_state(S3Task(operation="put_object", bucket="b", key="k", body="data"))
        assert s["Resource"] == "arn:aws:states:::aws-sdk:s3:putObject"
        assert s["Arguments"]["Bucket"] == "b"
        assert s["Arguments"]["Body"] == "data"

    def test_s3_unknown_operation_defaults_to_get(self):
        s = _generate_step_state(S3Task(operation="frobnicate", bucket="b", key="k"))
        assert s["Resource"] == "arn:aws:states:::aws-sdk:s3:getObject"

    def test_glue_async_when_no_wait(self):
        s = _generate_step_state(GlueTask(job_name="job", wait_for_completion=False))
        assert s["Resource"] == "arn:aws:states:::glue:startJobRun"
        assert s["Arguments"]["JobName"] == "job"

    def test_glue_sync_is_default(self):
        # wait_for_completion defaults to True → .sync variant.
        s = _generate_step_state(GlueTask(job_name="job"))
        assert s["Resource"] == "arn:aws:states:::glue:startJobRun.sync"

    def test_athena(self):
        # Default waits for completion → .sync.
        s = _generate_step_state(AthenaTask(
            query_string="SELECT 1", database="db", output_location="s3://bucket/results/",
        ))
        assert s["Resource"] == "arn:aws:states:::athena:startQueryExecution.sync"
        assert s["Arguments"]["QueryString"] == "SELECT 1"
        assert s["Arguments"]["QueryExecutionContext"]["Database"] == "db"

    def test_ecs_fargate_networking(self):
        s = _generate_step_state(ECSTask(
            cluster="c", task_definition="td", launch_type="FARGATE", subnets=["subnet-1"],
        ))
        # Default waits for completion → .sync.
        assert s["Resource"] == "arn:aws:states:::ecs:runTask.sync"
        net = s["Arguments"]["NetworkConfiguration"]["AwsvpcConfiguration"]
        assert net["Subnets"] == ["subnet-1"]

    def test_ecs_async_when_no_wait(self):
        s = _generate_step_state(ECSTask(
            cluster="c", task_definition="td", subnets=["subnet-1"], wait_for_completion=False,
        ))
        assert s["Resource"] == "arn:aws:states:::ecs:runTask"

    def test_eventbridge(self):
        s = _generate_step_state(EventBridgeTask(
            event_bus="default", source="my.app", detail_type="OrderPlaced", detail="{}",
        ))
        assert s["Resource"] == "arn:aws:states:::events:putEvents"
        entry = s["Arguments"]["Entries"][0]
        assert entry["Source"] == "my.app"
        assert entry["DetailType"] == "OrderPlaced"

    def test_bedrock(self):
        s = _generate_step_state(BedrockTask(
            model_id="anthropic.claude", body={"prompt": "hi"},
            content_type="application/json", accept="application/json",
        ))
        assert s["Resource"] == "arn:aws:states:::bedrock:invokeModel"
        assert s["Arguments"]["ModelId"] == "anthropic.claude"

    def test_http(self):
        s = _generate_step_state(HttpTask(
            url="https://api.example.com", method="POST",
            headers={"X-Key": "1"}, connection_arn="arn:conn",
        ))
        assert s["Resource"] == "arn:aws:states:::http:invoke"
        assert s["Arguments"]["ApiEndpoint"] == "https://api.example.com"
        assert s["Arguments"]["Method"] == "POST"
        assert s["Arguments"]["Authentication"]["ConnectionArn"] == "arn:conn"


# ============================================================ #
# construction-time validation — regression tests for a
# systemic gap found in a code-review pass: none of the 9
# AWS-service Step classes (plus HttpTask) validated their
# required fields at construction, even though every one of
# them is unconditionally embedded into the generated ASL
# Arguments by its _gen_*_state function. A missing required
# field (empty string default) previously passed local
# validate_asl_from_dag cleanly and only failed at actual AWS
# execution time. Each test below reproduces the exact gap
# found, confirms it's now caught immediately at construction.
# ============================================================ #
class TestServiceTaskConstructionValidation:
    def test_http_task_requires_url(self):
        with pytest.raises(ValueError, match="requires 'url'"):
            HttpTask()

    def test_dynamodb_requires_table_name(self):
        with pytest.raises(ValueError, match="requires 'table_name'"):
            DynamoDBTask(operation="get_item", key={"pk": {"S": "x"}})

    def test_dynamodb_get_item_requires_key(self):
        with pytest.raises(ValueError, match="requires 'key'"):
            DynamoDBTask(table_name="t", operation="get_item")

    def test_dynamodb_put_item_requires_item(self):
        with pytest.raises(ValueError, match="requires 'item'"):
            DynamoDBTask(table_name="t", operation="put_item")

    def test_dynamodb_update_item_requires_update_expression(self):
        with pytest.raises(ValueError, match="requires 'update_expression'"):
            DynamoDBTask(table_name="t", operation="update_item")

    def test_dynamodb_query_requires_key_condition(self):
        with pytest.raises(ValueError, match="requires 'key_condition'"):
            DynamoDBTask(table_name="t", operation="query")

    def test_dynamodb_scan_needs_nothing_extra(self):
        # scan has no operation-specific requirement beyond table_name.
        d = DynamoDBTask(table_name="t", operation="scan")
        assert d.table_name == "t"

    def test_sns_requires_topic_arn(self):
        with pytest.raises(ValueError, match="requires 'topic_arn'"):
            SNSTask(message="hi")

    def test_sns_requires_message(self):
        with pytest.raises(ValueError, match="requires 'message'"):
            SNSTask(topic_arn="arn:aws:sns:x")

    def test_sqs_requires_queue_url(self):
        with pytest.raises(ValueError, match="requires 'queue_url'"):
            SQSTask()

    def test_s3_requires_bucket(self):
        with pytest.raises(ValueError, match="requires 'bucket'"):
            S3Task(key="k")

    def test_s3_requires_key(self):
        with pytest.raises(ValueError, match="requires 'key'"):
            S3Task(bucket="b")

    def test_s3_put_object_requires_body(self):
        with pytest.raises(ValueError, match="requires 'body'"):
            S3Task(bucket="b", key="k", operation="put_object")

    def test_s3_copy_object_requires_copy_source(self):
        with pytest.raises(ValueError, match="requires 'copy_source'"):
            S3Task(bucket="b", key="k", operation="copy_object")

    def test_glue_requires_job_name(self):
        with pytest.raises(ValueError, match="requires 'job_name'"):
            GlueTask()

    def test_athena_requires_query_string(self):
        with pytest.raises(ValueError, match="requires 'query_string'"):
            AthenaTask(database="d", output_location="s3://b/")

    def test_athena_requires_database(self):
        with pytest.raises(ValueError, match="requires 'database'"):
            AthenaTask(query_string="SELECT 1", output_location="s3://b/")

    def test_athena_requires_output_location(self):
        with pytest.raises(ValueError, match="requires 'output_location'"):
            AthenaTask(query_string="SELECT 1", database="d")

    def test_ecs_requires_cluster(self):
        with pytest.raises(ValueError, match="requires 'cluster'"):
            ECSTask(task_definition="td")

    def test_ecs_requires_task_definition(self):
        with pytest.raises(ValueError, match="requires 'task_definition'"):
            ECSTask(cluster="c")

    def test_ecs_fargate_requires_subnets(self):
        """Same gap as @task.ecs()'s check (already tested elsewhere), found
        independently missing on this separate, direct-Step construction
        path — a user hitting either constructor gets the same guardrail."""
        with pytest.raises(ValueError, match="requires subnets"):
            ECSTask(cluster="c", task_definition="td")  # FARGATE is the default

    def test_eventbridge_requires_source(self):
        with pytest.raises(ValueError, match="requires 'source'"):
            EventBridgeTask(detail_type="Foo")

    def test_eventbridge_requires_detail_type(self):
        with pytest.raises(ValueError, match="requires 'detail_type'"):
            EventBridgeTask(source="my.app")

    def test_bedrock_requires_model_id(self):
        with pytest.raises(ValueError, match="requires 'model_id'"):
            BedrockTask(body={"prompt": "hi"})

    def test_bedrock_requires_body(self):
        with pytest.raises(ValueError, match="requires 'body'"):
            BedrockTask(model_id="anthropic.claude-3-sonnet")
