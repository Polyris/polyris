"""Tests for polyris.resource_check — pre-deploy AWS resource reference check.

Coverage targets:
  - _collect_resource_refs: per task type, cross-account, ${...}, emr/sagemaker skip
  - Per-service check functions: found / not-found / empty-list (ECS/Batch)
  - check_resource_references: cross-account skip, cfn skip, dedup, multi-region,
    AccessDenied/Throttle/BotoCoreError → warning, not-found → error
  - print_resource_check_result: output format via capsys
  - Integration: deploy_pipeline calls check; --destroy skips it
  - Completeness: every TaskTypeLiteral covered by _RESOURCE_CHECKERS or skip set
"""
import typing
import pytest
from botocore.exceptions import BotoCoreError, ClientError

import polyris.resource_check as rc
from polyris.resource_check import (
    ResourceRef,
    ResourceCheckResult,
    _collect_resource_refs,
    _RESOURCE_CHECKERS,
    _RESOURCE_SKIP_TYPES,
    _TASK_TYPE_TO_KINDS,
    _KIND_TO_SERVICE,
    _IAM_ACTION,
    _KIND_DISPLAY,
    _ACCESS_DENIED_CODES,
    _CANNOT_VERIFY_CODES,
    _check_ecs_cluster,
    _check_ecs_task_def,
    _check_batch_job_queue,
    _check_batch_job_def,
    _NotFoundError,
    check_resource_references,
    print_resource_check_result,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client_error(code: str, message: str = "mock") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "op")


def _make_task(mocker, *, task_type: str, task_id: str = "t1", role: str = "same", **fields):
    t = mocker.MagicMock()
    t.task_type = task_type
    t.task_id = task_id
    t.role = role
    t.arn = fields.get("arn", "")
    t.function_name = fields.get("function_name", "")
    t.job_name = fields.get("job_name", "")
    t.cluster = fields.get("cluster", "")
    t.task_definition = fields.get("task_definition", "")
    t.workgroup = fields.get("workgroup", "primary")
    t.emr_cluster_id = fields.get("emr_cluster_id", "")
    t.job_queue = fields.get("job_queue", "")
    t.job_definition = fields.get("job_definition", "")
    return t


def _make_dag(mocker, tasks: list):
    dag = mocker.MagicMock()
    dag.tasks = tasks
    return dag


# ---------------------------------------------------------------------------
# _collect_resource_refs
# ---------------------------------------------------------------------------

class TestCollectResourceRefs:
    def test_sfn_with_arn(self, mocker):
        task = _make_task(mocker, task_type="sfn", arn="arn:aws:states:us-east-1:123:stateMachine:my-sfn")
        refs = _collect_resource_refs(_make_dag(mocker, [task]))
        assert len(refs) == 1
        assert refs[0].kind == "sfn"
        assert refs[0].ref == "arn:aws:states:us-east-1:123:stateMachine:my-sfn"
        assert refs[0].task_id == "t1"
        assert refs[0].role == "same"

    def test_sfn_cfn_substitution_still_collected(self, mocker):
        # ${...} is collected; filtering happens in check_resource_references
        task = _make_task(mocker, task_type="sfn", arn="${workflow_arn}")
        refs = _collect_resource_refs(_make_dag(mocker, [task]))
        assert len(refs) == 1
        assert refs[0].ref == "${workflow_arn}"

    def test_sfn_empty_arn_not_collected(self, mocker):
        task = _make_task(mocker, task_type="sfn", arn="")
        refs = _collect_resource_refs(_make_dag(mocker, [task]))
        assert refs == []

    def test_lambda_with_arn(self, mocker):
        task = _make_task(mocker, task_type="lambda", arn="arn:aws:lambda:us-east-1:123:function:my-func")
        refs = _collect_resource_refs(_make_dag(mocker, [task]))
        assert len(refs) == 1
        assert refs[0].kind == "lambda"
        assert refs[0].ref == "arn:aws:lambda:us-east-1:123:function:my-func"

    def test_lambda_with_function_name(self, mocker):
        task = _make_task(mocker, task_type="lambda", function_name="my-func")
        refs = _collect_resource_refs(_make_dag(mocker, [task]))
        assert len(refs) == 1
        assert refs[0].kind == "lambda"
        assert refs[0].ref == "my-func"

    def test_lambda_prefers_arn_over_function_name(self, mocker):
        task = _make_task(mocker, task_type="lambda",
                          arn="arn:aws:lambda:us-east-1:123:function:my-func",
                          function_name="my-func")
        refs = _collect_resource_refs(_make_dag(mocker, [task]))
        assert len(refs) == 1
        assert refs[0].ref == "arn:aws:lambda:us-east-1:123:function:my-func"

    def test_glue_with_job_name(self, mocker):
        task = _make_task(mocker, task_type="glue", job_name="etl-job")
        refs = _collect_resource_refs(_make_dag(mocker, [task]))
        assert len(refs) == 1
        assert refs[0].kind == "glue"
        assert refs[0].ref == "etl-job"

    def test_ecs_collects_cluster_and_task_def(self, mocker):
        task = _make_task(mocker, task_type="ecs",
                          cluster="my-cluster", task_definition="my-task-def")
        refs = _collect_resource_refs(_make_dag(mocker, [task]))
        assert len(refs) == 2
        kinds = {r.kind for r in refs}
        assert kinds == {"ecs_cluster", "ecs_task_definition"}

    def test_ecs_only_cluster_when_no_task_def(self, mocker):
        task = _make_task(mocker, task_type="ecs", cluster="my-cluster", task_definition="")
        refs = _collect_resource_refs(_make_dag(mocker, [task]))
        assert len(refs) == 1
        assert refs[0].kind == "ecs_cluster"

    def test_athena_custom_workgroup(self, mocker):
        task = _make_task(mocker, task_type="athena", workgroup="my-wg")
        refs = _collect_resource_refs(_make_dag(mocker, [task]))
        assert len(refs) == 1
        assert refs[0].kind == "athena_workgroup"
        assert refs[0].ref == "my-wg"

    def test_athena_primary_workgroup_not_collected(self, mocker):
        task = _make_task(mocker, task_type="athena", workgroup="primary")
        refs = _collect_resource_refs(_make_dag(mocker, [task]))
        assert refs == []

    def test_athena_empty_workgroup_not_collected(self, mocker):
        task = _make_task(mocker, task_type="athena", workgroup="")
        refs = _collect_resource_refs(_make_dag(mocker, [task]))
        assert refs == []

    def test_batch_collects_queue_and_definition(self, mocker):
        task = _make_task(mocker, task_type="batch",
                          job_queue="my-queue", job_definition="my-job-def")
        refs = _collect_resource_refs(_make_dag(mocker, [task]))
        assert len(refs) == 2
        kinds = {r.kind for r in refs}
        assert kinds == {"batch_job_queue", "batch_job_definition"}

    def test_emr_not_collected(self, mocker):
        task = _make_task(mocker, task_type="emr", emr_cluster_id="j-ABC123")
        refs = _collect_resource_refs(_make_dag(mocker, [task]))
        assert refs == []

    def test_sagemaker_not_collected(self, mocker):
        task = _make_task(mocker, task_type="sagemaker")
        refs = _collect_resource_refs(_make_dag(mocker, [task]))
        assert refs == []

    def test_cross_account_task_still_collected(self, mocker):
        task = _make_task(mocker, task_type="glue", job_name="etl-job",
                          role="arn:aws:iam::222222222222:role/etl")
        refs = _collect_resource_refs(_make_dag(mocker, [task]))
        assert len(refs) == 1
        assert refs[0].role == "arn:aws:iam::222222222222:role/etl"

    def test_lambda_both_empty_not_collected(self, mocker):
        task = _make_task(mocker, task_type="lambda", arn="", function_name="")
        refs = _collect_resource_refs(_make_dag(mocker, [task]))
        assert refs == []

    def test_glue_empty_job_name_not_collected(self, mocker):
        task = _make_task(mocker, task_type="glue", job_name="")
        refs = _collect_resource_refs(_make_dag(mocker, [task]))
        assert refs == []

    def test_ecs_only_task_def_when_no_cluster(self, mocker):
        task = _make_task(mocker, task_type="ecs", cluster="", task_definition="my-task-def")
        refs = _collect_resource_refs(_make_dag(mocker, [task]))
        assert len(refs) == 1
        assert refs[0].kind == "ecs_task_definition"

    def test_batch_only_job_def_when_no_queue(self, mocker):
        task = _make_task(mocker, task_type="batch", job_queue="", job_definition="my-job-def")
        refs = _collect_resource_refs(_make_dag(mocker, [task]))
        assert len(refs) == 1
        assert refs[0].kind == "batch_job_definition"

    def test_unknown_task_type_raises_value_error(self, mocker):
        task = _make_task(mocker, task_type="unknown_future_type")
        with pytest.raises(ValueError, match="unknown_future_type.*not recognised"):
            _collect_resource_refs(_make_dag(mocker, [task]))

    def test_empty_dag(self, mocker):
        refs = _collect_resource_refs(_make_dag(mocker, []))
        assert refs == []


# ---------------------------------------------------------------------------
# Per-service check functions
# ---------------------------------------------------------------------------

class TestCheckFunctions:
    def test_check_sfn_found(self, mocker):
        client = mocker.MagicMock()
        rc._check_sfn(client, "arn:aws:states:us-east-1:123:stateMachine:ok")
        client.describe_state_machine.assert_called_once_with(
            stateMachineArn="arn:aws:states:us-east-1:123:stateMachine:ok"
        )

    def test_check_sfn_not_found_raises(self, mocker):
        client = mocker.MagicMock()
        client.describe_state_machine.side_effect = _client_error("StateMachineDoesNotExist")
        with pytest.raises(ClientError):
            rc._check_sfn(client, "arn:aws:states:us-east-1:123:stateMachine:missing")

    def test_check_lambda_found(self, mocker):
        client = mocker.MagicMock()
        rc._check_lambda(client, "my-func")
        client.get_function.assert_called_once_with(FunctionName="my-func")

    def test_check_lambda_not_found_raises(self, mocker):
        client = mocker.MagicMock()
        client.get_function.side_effect = _client_error("ResourceNotFoundException")
        with pytest.raises(ClientError):
            rc._check_lambda(client, "missing-func")

    def test_check_lambda_bad_arn_raises_client_error(self, mocker):
        client = mocker.MagicMock()
        client.get_function.side_effect = _client_error("InvalidParameterValueException")
        with pytest.raises(ClientError):
            rc._check_lambda(client, "not-an-arn:bad")

    def test_check_glue_found(self, mocker):
        client = mocker.MagicMock()
        rc._check_glue_job(client, "my-job")
        client.get_job.assert_called_once_with(JobName="my-job")

    def test_check_glue_not_found_raises(self, mocker):
        client = mocker.MagicMock()
        client.get_job.side_effect = _client_error("EntityNotFoundException")
        with pytest.raises(ClientError):
            rc._check_glue_job(client, "missing-job")

    def test_check_ecs_cluster_found(self, mocker):
        client = mocker.MagicMock()
        client.describe_clusters.return_value = {"clusters": [{"clusterName": "my-cluster", "status": "ACTIVE"}]}
        _check_ecs_cluster(client, "my-cluster")  # no raise

    def test_check_ecs_cluster_empty_list_raises_not_found(self, mocker):
        client = mocker.MagicMock()
        client.describe_clusters.return_value = {"clusters": []}
        with pytest.raises(_NotFoundError):
            _check_ecs_cluster(client, "ghost-cluster")

    def test_check_ecs_cluster_inactive_raises_not_found(self, mocker):
        """AWS returns INACTIVE clusters in the list when queried by name; must be rejected."""
        client = mocker.MagicMock()
        client.describe_clusters.return_value = {"clusters": [{"clusterName": "old-cluster", "status": "INACTIVE"}]}
        with pytest.raises(_NotFoundError):
            _check_ecs_cluster(client, "old-cluster")

    def test_check_ecs_task_def_found(self, mocker):
        client = mocker.MagicMock()
        client.describe_task_definition.return_value = {
            "taskDefinition": {"taskDefinitionArn": "arn:...", "status": "ACTIVE"}
        }
        rc._check_ecs_task_def(client, "my-task-def")
        client.describe_task_definition.assert_called_once_with(taskDefinition="my-task-def")

    def test_check_ecs_task_def_not_found_raises(self, mocker):
        client = mocker.MagicMock()
        client.describe_task_definition.side_effect = _client_error("ClientException")
        with pytest.raises(ClientError):
            rc._check_ecs_task_def(client, "missing-def")

    def test_check_ecs_task_def_inactive_raises_not_found(self, mocker):
        """Deregistered task definitions have status=INACTIVE — unusable at runtime."""
        client = mocker.MagicMock()
        client.describe_task_definition.return_value = {
            "taskDefinition": {"status": "INACTIVE"}
        }
        with pytest.raises(_NotFoundError):
            _check_ecs_task_def(client, "old-task-def")

    def test_check_ecs_task_def_delete_in_progress_raises_not_found(self, mocker):
        client = mocker.MagicMock()
        client.describe_task_definition.return_value = {
            "taskDefinition": {"status": "DELETE_IN_PROGRESS"}
        }
        with pytest.raises(_NotFoundError):
            _check_ecs_task_def(client, "deleting-task-def")

    def test_check_batch_job_queue_found(self, mocker):
        client = mocker.MagicMock()
        client.describe_job_queues.return_value = {"jobQueues": [{"jobQueueName": "q", "status": "VALID"}]}
        _check_batch_job_queue(client, "q")  # no raise

    def test_check_batch_job_queue_empty_list_raises_not_found(self, mocker):
        client = mocker.MagicMock()
        client.describe_job_queues.return_value = {"jobQueues": []}
        with pytest.raises(_NotFoundError):
            _check_batch_job_queue(client, "ghost-queue")

    def test_check_batch_job_queue_deleting_raises_not_found(self, mocker):
        """A queue being deleted is no longer usable for new job submissions."""
        client = mocker.MagicMock()
        client.describe_job_queues.return_value = {"jobQueues": [{"jobQueueName": "q", "status": "DELETING"}]}
        with pytest.raises(_NotFoundError):
            _check_batch_job_queue(client, "q")

    def test_check_batch_job_queue_invalid_raises_not_found(self, mocker):
        client = mocker.MagicMock()
        client.describe_job_queues.return_value = {"jobQueues": [{"jobQueueName": "q", "status": "INVALID"}]}
        with pytest.raises(_NotFoundError):
            _check_batch_job_queue(client, "q")

    def test_check_batch_job_def_found(self, mocker):
        client = mocker.MagicMock()
        client.describe_job_definitions.return_value = {"jobDefinitions": [{"jobDefinitionName": "jd", "status": "ACTIVE"}]}
        _check_batch_job_def(client, "jd")  # no raise
        client.describe_job_definitions.assert_called_once_with(jobDefinitions=["jd"], status="ACTIVE")

    def test_check_batch_job_def_empty_list_raises_not_found(self, mocker):
        client = mocker.MagicMock()
        client.describe_job_definitions.return_value = {"jobDefinitions": []}
        with pytest.raises(_NotFoundError):
            _check_batch_job_def(client, "ghost-def")

    def test_check_batch_job_def_inactive_raises_not_found(self, mocker):
        """INACTIVE defs are excluded by status='ACTIVE' — AWS returns empty list,
        so a deregistered job def correctly fails the check instead of silently passing."""
        client = mocker.MagicMock()
        client.describe_job_definitions.return_value = {"jobDefinitions": []}
        with pytest.raises(_NotFoundError):
            _check_batch_job_def(client, "inactive-def")
        client.describe_job_definitions.assert_called_once_with(
            jobDefinitions=["inactive-def"], status="ACTIVE"
        )

    def test_check_athena_workgroup_found(self, mocker):
        client = mocker.MagicMock()
        rc._check_athena_workgroup(client, "my-wg")
        client.get_work_group.assert_called_once_with(WorkGroup="my-wg")

    def test_check_athena_workgroup_not_found_raises(self, mocker):
        client = mocker.MagicMock()
        client.get_work_group.side_effect = _client_error("InvalidRequestException")
        with pytest.raises(ClientError):
            rc._check_athena_workgroup(client, "missing-wg")


# ---------------------------------------------------------------------------
# check_resource_references
# ---------------------------------------------------------------------------

class TestCheckResourceReferences:
    def _session(self, mocker, client_mock=None):
        session = mocker.MagicMock()
        if client_mock is not None:
            session.client.return_value = client_mock
        return session

    def test_empty_dag_returns_empty_result(self, mocker):
        result = check_resource_references(
            _make_dag(mocker, []),
            self._session(mocker),
            "us-east-1",
        )
        assert result.errors == []
        assert result.warnings == []
        assert result.skipped == []
        assert result._checked == 0

    def test_cfn_substitution_silently_skipped(self, mocker):
        task = _make_task(mocker, task_type="sfn", arn="${workflow_arn}")
        result = check_resource_references(
            _make_dag(mocker, [task]),
            self._session(mocker),
            "us-east-1",
        )
        assert result.errors == []
        assert result.warnings == []
        assert result.skipped == []
        assert result._checked == 0

    def test_cross_account_task_goes_to_skipped(self, mocker):
        task = _make_task(mocker, task_type="glue", job_name="etl",
                          role="arn:aws:iam::222:role/etl")
        result = check_resource_references(
            _make_dag(mocker, [task]),
            self._session(mocker),
            "us-east-1",
        )
        assert len(result.skipped) == 1
        assert result.skipped[0].role == "arn:aws:iam::222:role/etl"
        assert result._checked == 0

    def test_named_role_alias_goes_to_skipped(self, mocker):
        """Named role aliases like 'acq' are treated the same as cross-account —
        the deploy session cannot assume them, so verification is skipped."""
        task = _make_task(mocker, task_type="glue", job_name="etl", role="acq")
        result = check_resource_references(
            _make_dag(mocker, [task]),
            self._session(mocker),
            "us-east-1",
        )
        assert len(result.skipped) == 1
        assert result.skipped[0].role == "acq"
        assert result._checked == 0

    def test_resource_found_no_errors(self, mocker):
        client = mocker.MagicMock()
        client.get_job.return_value = {"Job": {"Name": "etl"}}
        task = _make_task(mocker, task_type="glue", job_name="etl")
        result = check_resource_references(
            _make_dag(mocker, [task]),
            self._session(mocker, client),
            "us-east-1",
        )
        assert result.errors == []
        assert result.warnings == []
        assert result._checked == 1

    def test_resource_not_found_goes_to_errors(self, mocker):
        client = mocker.MagicMock()
        client.get_job.side_effect = _client_error("EntityNotFoundException")
        task = _make_task(mocker, task_type="glue", job_name="missing-job")
        result = check_resource_references(
            _make_dag(mocker, [task]),
            self._session(mocker, client),
            "us-east-1",
        )
        assert len(result.errors) == 1
        assert result.errors[0].ref == "missing-job"

    def test_access_denied_goes_to_warnings_and_access_denied(self, mocker):
        client = mocker.MagicMock()
        client.get_job.side_effect = _client_error("AccessDeniedException")
        task = _make_task(mocker, task_type="glue", job_name="my-job")
        result = check_resource_references(
            _make_dag(mocker, [task]),
            self._session(mocker, client),
            "us-east-1",
        )
        assert len(result.warnings) == 1
        assert len(result.access_denied) == 1
        assert result.errors == []

    def test_throttling_goes_to_warnings_not_access_denied(self, mocker):
        """Throttle is cannot-verify but NOT an IAM issue — must not appear in access_denied."""
        client = mocker.MagicMock()
        client.get_job.side_effect = _client_error("ThrottlingException")
        task = _make_task(mocker, task_type="glue", job_name="my-job")
        result = check_resource_references(
            _make_dag(mocker, [task]),
            self._session(mocker, client),
            "us-east-1",
        )
        assert len(result.warnings) == 1
        assert result.access_denied == []

    def test_kms_access_denied_goes_to_warnings_not_errors(self, mocker):
        """KMS denial on a KMS-encrypted SFN must not abort the deploy (false-positive guard)."""
        client = mocker.MagicMock()
        client.describe_state_machine.side_effect = _client_error("KmsAccessDeniedException")
        task = _make_task(mocker, task_type="sfn",
                          arn="arn:aws:states:us-east-1:123:stateMachine:encrypted")
        result = check_resource_references(
            _make_dag(mocker, [task]),
            self._session(mocker, client),
            "us-east-1",
        )
        assert len(result.warnings) == 1
        assert result.errors == []

    def test_botocore_error_goes_to_warnings(self, mocker):
        client = mocker.MagicMock()
        client.get_job.side_effect = BotoCoreError()
        task = _make_task(mocker, task_type="glue", job_name="my-job")
        result = check_resource_references(
            _make_dag(mocker, [task]),
            self._session(mocker, client),
            "us-east-1",
        )
        assert len(result.warnings) == 1
        assert result.errors == []

    def test_deduplication_one_api_call_per_unique_ref(self, mocker):
        client = mocker.MagicMock()
        client.get_job.return_value = {}
        tasks = [
            _make_task(mocker, task_type="glue", job_name="etl", task_id=f"t{i}")
            for i in range(5)
        ]
        result = check_resource_references(
            _make_dag(mocker, tasks),
            self._session(mocker, client),
            "us-east-1",
        )
        assert client.get_job.call_count == 1
        assert result._checked == 1

    def test_multi_region_arn_uses_arns_region(self, mocker):
        session = mocker.MagicMock()
        sfn_client = mocker.MagicMock()
        session.client.return_value = sfn_client

        task = _make_task(mocker, task_type="sfn",
                          arn="arn:aws:states:us-east-1:123:stateMachine:my-sfn")
        # Deploy session is eu-west-1 but the SFN is in us-east-1
        check_resource_references(
            _make_dag(mocker, [task]),
            session,
            "eu-west-1",
        )
        session.client.assert_called_once_with("stepfunctions", region_name="us-east-1")

    def test_name_based_ref_uses_deploy_region(self, mocker):
        session = mocker.MagicMock()
        client = mocker.MagicMock()
        session.client.return_value = client

        task = _make_task(mocker, task_type="glue", job_name="etl-job")
        check_resource_references(
            _make_dag(mocker, [task]),
            session,
            "eu-west-1",
        )
        session.client.assert_called_once_with("glue", region_name="eu-west-1")

    def test_ecs_cluster_empty_list_goes_to_errors(self, mocker):
        client = mocker.MagicMock()
        client.describe_clusters.return_value = {"clusters": []}
        task = _make_task(mocker, task_type="ecs",
                          cluster="ghost-cluster", task_definition="")
        result = check_resource_references(
            _make_dag(mocker, [task]),
            self._session(mocker, client),
            "us-east-1",
        )
        assert len(result.errors) == 1

    def test_batch_queue_empty_list_goes_to_errors(self, mocker):
        client = mocker.MagicMock()
        client.describe_job_queues.return_value = {"jobQueues": []}
        task = _make_task(mocker, task_type="batch",
                          job_queue="ghost-queue", job_definition="")
        result = check_resource_references(
            _make_dag(mocker, [task]),
            self._session(mocker, client),
            "us-east-1",
        )
        assert len(result.errors) == 1

    def test_lambda_invalid_arn_goes_to_errors(self, mocker):
        client = mocker.MagicMock()
        client.get_function.side_effect = _client_error("InvalidParameterValueException")
        task = _make_task(mocker, task_type="lambda", arn="not-valid")
        result = check_resource_references(
            _make_dag(mocker, [task]),
            self._session(mocker, client),
            "us-east-1",
        )
        assert len(result.errors) == 1
        assert result.warnings == []

    def test_region_none_falls_back_to_us_east_1(self, mocker):
        """When region=None and session has no region, falls back to us-east-1."""
        session = mocker.MagicMock()
        session.region_name = None  # simulate no env/profile region configured
        client = mocker.MagicMock()
        session.client.return_value = client
        client.get_job.return_value = {}

        task = _make_task(mocker, task_type="glue", job_name="etl-job")
        check_resource_references(_make_dag(mocker, [task]), session, None)
        session.client.assert_called_once_with("glue", region_name="us-east-1")

    def test_region_none_uses_session_region_when_configured(self, mocker):
        """When region=None but session has a configured region, that region is used."""
        session = mocker.MagicMock()
        session.region_name = "eu-west-1"  # configured via AWS_DEFAULT_REGION or profile
        client = mocker.MagicMock()
        session.client.return_value = client
        client.get_job.return_value = {}

        task = _make_task(mocker, task_type="glue", job_name="etl-job")
        check_resource_references(_make_dag(mocker, [task]), session, None)
        session.client.assert_called_once_with("glue", region_name="eu-west-1")

    def test_client_cache_hit_reuses_client(self, mocker):
        """Two different refs on the same service+region share one client instance."""
        session = mocker.MagicMock()
        client = mocker.MagicMock()
        session.client.return_value = client
        client.get_job.return_value = {}

        tasks = [
            _make_task(mocker, task_type="glue", job_name="job-a", task_id="t1"),
            _make_task(mocker, task_type="glue", job_name="job-b", task_id="t2"),
        ]
        result = check_resource_references(_make_dag(mocker, tasks), session, "us-east-1")
        assert session.client.call_count == 1
        assert client.get_job.call_count == 2
        assert result._checked == 2


# ---------------------------------------------------------------------------
# print_resource_check_result (output format)
# ---------------------------------------------------------------------------

class TestPrintResourceCheckResult:
    def test_empty_result_prints_nothing(self, capsys):
        print_resource_check_result(ResourceCheckResult())
        assert capsys.readouterr().out == ""

    def test_success_prints_checkmark(self, capsys, mocker):
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = ResourceCheckResult(_checked=3)
        print_resource_check_result(result)
        out = capsys.readouterr().out
        assert "Checking referenced AWS resources..." in out
        assert "✓" in out

    def test_errors_print_x_lines_and_abort(self, capsys, mocker):
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = ResourceCheckResult(
            errors=[ResourceRef(task_id="process", kind="sfn",
                                ref="arn:aws:states:us-east-1:123:stateMachine:missing")],
            _checked=1,
        )
        print_resource_check_result(result)
        out = capsys.readouterr().out
        assert "✗" in out
        assert "process" in out
        assert "does not exist" in out
        assert "aborting deploy" in out

    def test_lifecycle_kind_errors_print_deregistered_message(self, capsys, mocker):
        """ECS/Batch errors use 'does not exist or is deregistered' — not bare 'does not exist'.

        Coverage tools do not track ternary branches; this test pins the message
        for _LIFECYCLE_KINDS so a mutation that removes the check is caught.
        """
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = ResourceCheckResult(
            errors=[ResourceRef(task_id="run_job", kind="ecs_cluster", ref="my-cluster")],
            _checked=1,
        )
        print_resource_check_result(result)
        out = capsys.readouterr().out
        assert "does not exist or is deregistered" in out
        assert "run_job" in out

    def test_non_lifecycle_kind_errors_print_simple_not_found(self, capsys, mocker):
        """Non-lifecycle kinds (sfn, lambda, glue, athena) use bare 'does not exist'."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = ResourceCheckResult(
            errors=[ResourceRef(task_id="t1", kind="lambda", ref="my-func")],
            _checked=1,
        )
        print_resource_check_result(result)
        out = capsys.readouterr().out
        assert "does not exist" in out
        assert "deregistered" not in out

    def test_single_resource_uses_singular_noun(self, capsys, mocker):
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = ResourceCheckResult(
            errors=[ResourceRef(task_id="t1", kind="glue", ref="missing-job")],
            _checked=1,
        )
        print_resource_check_result(result)
        out = capsys.readouterr().out
        assert "1 resource does not exist" in out

    def test_multiple_resources_uses_plural_noun(self, capsys, mocker):
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = ResourceCheckResult(
            errors=[
                ResourceRef(task_id="t1", kind="glue", ref="job1"),
                ResourceRef(task_id="t2", kind="glue", ref="job2"),
            ],
            _checked=2,
        )
        print_resource_check_result(result)
        out = capsys.readouterr().out
        assert "2 resources do not exist" in out

    def test_warnings_print_block_with_iam_hint_only_for_access_denied(self, capsys, mocker):
        """IAM hint shown only when warning was caused by an IAM denial, not throttle."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        rr = ResourceRef(task_id="load", kind="lambda", ref="my-func")
        result = ResourceCheckResult(
            warnings=[rr],
            access_denied=[rr],
            _checked=1,
        )
        print_resource_check_result(result)
        out = capsys.readouterr().out
        assert "could not be verified" in out
        assert "lambda:GetFunction" in out
        assert "load" in out

    def test_throttle_warning_prints_no_iam_hint(self, capsys, mocker):
        """A throttle warning must not tell the operator to change their IAM policy."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = ResourceCheckResult(
            warnings=[ResourceRef(task_id="load", kind="lambda", ref="my-func")],
            access_denied=[],  # throttle — not an IAM issue
            _checked=1,
        )
        print_resource_check_result(result)
        out = capsys.readouterr().out
        assert "could not be verified" in out
        assert "lambda:GetFunction" not in out
        assert "add " not in out

    def test_warnings_unknown_kind_no_iam_hint(self, capsys, mocker):
        """When a warning's kind is not in _IAM_ACTION, the IAM hint line is omitted."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        rr = ResourceRef(task_id="t1", kind="unknown_kind", ref="my-ref")
        result = ResourceCheckResult(
            warnings=[rr],
            access_denied=[rr],
            _checked=1,
        )
        print_resource_check_result(result)
        out = capsys.readouterr().out
        assert "could not be verified" in out
        assert "add " not in out

    def test_cross_account_prints_role(self, capsys, mocker):
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = ResourceCheckResult(
            skipped=[ResourceRef(task_id="cross_task", kind="sfn",
                                 ref="arn:aws:states:us-east-1:222:stateMachine:cross",
                                 role="arn:aws:iam::222:role/etl")],
        )
        print_resource_check_result(result)
        out = capsys.readouterr().out
        assert "different IAM role" in out
        assert "arn:aws:iam::222:role/etl" in out
        assert "cross_task" in out

    def test_named_role_alias_prints_role_message(self, capsys, mocker):
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = ResourceCheckResult(
            skipped=[ResourceRef(task_id="my_task", kind="glue",
                                 ref="etl-job", role="acq")],
        )
        print_resource_check_result(result)
        out = capsys.readouterr().out
        assert "different IAM role" in out
        assert "acq" in out
        assert "my_task" in out

    def test_cross_account_truncation(self, capsys, mocker):
        mocker.patch("sys.stdout.isatty", return_value=False)
        skipped = [
            ResourceRef(task_id=f"t{i}", kind="glue", ref=f"job{i}",
                        role="arn:aws:iam::222:role/etl")
            for i in range(12)
        ]
        result = ResourceCheckResult(skipped=skipped)
        print_resource_check_result(result)
        out = capsys.readouterr().out
        assert "... and 2 more" in out

    def test_mixed_output(self, capsys, mocker):
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = ResourceCheckResult(
            errors=[ResourceRef(task_id="t1", kind="sfn", ref="arn:bad")],
            warnings=[ResourceRef(task_id="t2", kind="glue", ref="my-job")],
            skipped=[ResourceRef(task_id="t3", kind="lambda", ref="fn",
                                 role="arn:aws:iam::222:role/etl")],
            _checked=2,
        )
        print_resource_check_result(result)
        out = capsys.readouterr().out
        assert "✗" in out
        assert "could not be verified" in out
        assert "different IAM role" in out
        assert "aborting deploy" in out

    def test_only_skipped_does_not_print_abort(self, capsys, mocker):
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = ResourceCheckResult(
            skipped=[ResourceRef(task_id="t1", kind="glue", ref="job",
                                 role="arn:aws:iam::222:role/etl")],
        )
        print_resource_check_result(result)
        out = capsys.readouterr().out
        assert "aborting deploy" not in out
        assert "different IAM role" in out


# ---------------------------------------------------------------------------
# Integration: deploy_pipeline calls the check; --destroy skips it
# ---------------------------------------------------------------------------

def _make_deploy_mocks(mocker, *, resource_result: ResourceCheckResult):
    """Patch deploy_pipeline up to (and including) the resource check step.

    The resource check is patched to return `resource_result`.
    Everything after (template generation, CFN deploy) raises SystemExit(2)
    so tests can distinguish "passed the check" from "aborted at check".
    """
    dag = mocker.MagicMock()
    dag.dag_id = "test-dag"
    dag.schedule = None
    dag.is_asset_triggered = False

    mocker.patch(
        "polyris.validation.validate_asl_from_dag",
        return_value=(True, [], []),
    )
    cfg = mocker.patch("polyris.deploy.polyris_config")
    cfg.stage = "dev"
    cfg.for_stage.return_value = mocker.MagicMock(
        stack_name="acme-dev",
        namespace="acme",
        region="us-east-1",
        profile=None,
        get=lambda k, default=None: None,
    )

    session = mocker.MagicMock()
    session.client.return_value.get_caller_identity.return_value = {"Account": "123"}
    session.client.return_value.describe_stacks.return_value = {
        "Stacks": [{"Outputs": [
            {"OutputKey": "DependencyWrapperArn", "OutputValue": "arn:wrapper"},
            {"OutputKey": "OrchestrationRoleArn", "OutputValue": "arn:role"},
            {"OutputKey": "PipelineRegistryTable", "OutputValue": "reg"},
            {"OutputKey": "PipelineTokensTable", "OutputValue": "tok"},
            {"OutputKey": "AssetSubscriptionsTable", "OutputValue": "asset"},
        ], "Parameters": []}]
    }
    mocker.patch("polyris.deploy.boto3").Session.return_value = session

    mocker.patch(
        "polyris.deploy.check_resource_references",
        return_value=resource_result,
    )
    mocker.patch("polyris.deploy.print_resource_check_result")

    # make the next step (template generation) raise SystemExit(2)
    mocker.patch(
        "polyris.deploy._generate_cfn_template",
        side_effect=SystemExit(2),
    )

    return dag


def test_deploy_pipeline_calls_resource_check(mocker):
    dag = _make_deploy_mocks(mocker, resource_result=ResourceCheckResult())
    with pytest.raises(SystemExit) as exc:
        from polyris import deploy
        deploy.deploy_pipeline(dag)
    # Passed the resource check (no errors), hit the next barrier
    assert exc.value.code == 2
    deploy.check_resource_references.assert_called_once()


def test_deploy_pipeline_exits_on_resource_errors(mocker):
    result = ResourceCheckResult(
        errors=[ResourceRef(task_id="t1", kind="sfn", ref="missing")],
        _checked=1,
    )
    dag = _make_deploy_mocks(mocker, resource_result=result)
    with pytest.raises(SystemExit) as exc:
        from polyris import deploy
        deploy.deploy_pipeline(dag)
    assert exc.value.code == 1


def test_deploy_pipeline_exits_cleanly_on_unknown_task_type(mocker):
    """ValueError from an unknown task_type must produce a clean error, not a traceback."""
    dag = _make_deploy_mocks(mocker, resource_result=ResourceCheckResult())
    mocker.patch(
        "polyris.deploy.check_resource_references",
        side_effect=ValueError("task_type 'kinesis' is not recognised by the pre-deploy resource check"),
    )
    with pytest.raises(SystemExit) as exc:
        from polyris import deploy
        deploy.deploy_pipeline(dag)
    assert exc.value.code == 1


def test_deploy_destroy_skips_resource_check(mocker):
    dag = mocker.MagicMock()
    dag.dag_id = "test-dag"

    cfg = mocker.patch("polyris.deploy.polyris_config")
    cfg.stage = "dev"
    cfg.for_stage.return_value = mocker.MagicMock(
        stack_name="acme-dev",
        namespace="acme",
        region="us-east-1",
        profile=None,
        get=lambda k, default=None: None,
    )

    sts = mocker.MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123"}
    cfn = mocker.MagicMock()
    cfn.describe_stack_events.return_value = {"StackEvents": []}
    cfn.get_waiter.return_value.wait.return_value = None
    cfn.describe_stacks.return_value = {
        "Stacks": [{"Outputs": [
            {"OutputKey": "DependencyWrapperArn", "OutputValue": "arn:wrapper"},
            {"OutputKey": "OrchestrationRoleArn", "OutputValue": "arn:role"},
        ], "Parameters": []}]
    }

    def _client(name, **kw):
        return {"sts": sts, "cloudformation": cfn}.get(name, mocker.MagicMock())

    session = mocker.MagicMock()
    session.client.side_effect = _client
    mocker.patch("polyris.deploy.boto3").Session.return_value = session

    rc_patch = mocker.patch("polyris.deploy.check_resource_references")

    from polyris import deploy
    deploy.deploy_pipeline(dag, destroy=True, dry_run=True)

    rc_patch.assert_not_called()


# ---------------------------------------------------------------------------
# Completeness: every TaskTypeLiteral covered or explicitly skipped
# ---------------------------------------------------------------------------

def test_every_task_type_has_checker_or_skip():
    """Adding a new TaskTypeLiteral without updating resource_check.py fails here."""
    from polyris.constants import TaskTypeLiteral
    all_types = set(typing.get_args(TaskTypeLiteral))

    # The two structures must form a clean partition — no type in both.
    overlap = _RESOURCE_SKIP_TYPES & set(_TASK_TYPE_TO_KINDS.keys())
    assert not overlap, f"Types in both skip set and kind map: {overlap}"

    uncovered = []
    for task_type in sorted(all_types):
        if task_type in _RESOURCE_SKIP_TYPES:
            continue
        kinds = _TASK_TYPE_TO_KINDS.get(task_type)
        if not kinds:
            uncovered.append(f"{task_type!r} missing from _TASK_TYPE_TO_KINDS")
            continue
        for kind in kinds:
            if kind not in _RESOURCE_CHECKERS:
                uncovered.append(f"kind {kind!r} (from {task_type!r}) missing from _RESOURCE_CHECKERS")
            if kind not in _KIND_TO_SERVICE:
                uncovered.append(f"kind {kind!r} (from {task_type!r}) missing from _KIND_TO_SERVICE")
            if kind not in _IAM_ACTION:
                uncovered.append(f"kind {kind!r} (from {task_type!r}) missing from _IAM_ACTION")
            if kind not in _KIND_DISPLAY:
                uncovered.append(f"kind {kind!r} (from {task_type!r}) missing from _KIND_DISPLAY")
    assert not uncovered, "\n".join(uncovered)


def test_access_denied_codes_is_strict_subset_of_cannot_verify_codes():
    """_ACCESS_DENIED_CODES must always be a strict subset of _CANNOT_VERIFY_CODES.

    The rule (polyris/CLAUDE.md): codes where the fix is 'add IAM permission' go in
    both sets; throttle/transient codes go in _CANNOT_VERIFY_CODES only.
    Collapsing the two sets would cause throttle errors to tell the operator to
    change their IAM policy — wrong guidance.
    """
    assert _ACCESS_DENIED_CODES < _CANNOT_VERIFY_CODES, (
        "_ACCESS_DENIED_CODES must be a strict subset of _CANNOT_VERIFY_CODES; "
        f"codes only in _ACCESS_DENIED_CODES: {_ACCESS_DENIED_CODES - _CANNOT_VERIFY_CODES}"
    )
