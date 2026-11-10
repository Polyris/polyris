"""Tests for polyris.resource_check — pre-deploy AWS resource reference check.

Coverage targets:
  - _collect_resource_refs: per task type, cross-account, ${...}, emr/sagemaker skip
  - Per-service check functions: found / not-found / empty-list (ECS/Batch);
    PROVISIONING ECS cluster, CREATING/UPDATING Batch queue treated as usable
  - check_resource_references: cross-account skip, cfn skip, dedup, multi-region,
    AccessDenied/Throttle/BotoCoreError → warning, not-found → error
  - _failed_refs: populated on errors + access_denied, not on throttle/BotoCoreError
  - print_resource_check_result: output format via capsys; ref/role/task_id sanitized
    in all three printer blocks (errors, warnings, skipped)
  - _sanitize: CSI ANSI strip, tab strip, newline/CR replace, truncation, non-string coercion
  - _numeric_str helper
  - _fetch_glue_data, _fetch_ecs_task_def_data, _fetch_batch_job_def_data
  - _check_glue_override, _check_ecs_override, _check_batch_override
  - check_resource_overrides: failed_refs skip, cross-account skip, cfn skip,
    dedup API calls, unverified dedup (unverified_seen), _skipped counter,
    ExpiredTokenException short-circuit + cached-resource still processed,
    BotoCoreError, expired_token flag
  - print_resource_override_result: output format via capsys; blank line before output,
    blank line between consecutive reports, blank line between reports and unverified,
    skip note in success tick, expired vs generic unverified message
  - Integration: deploy_pipeline calls both checks; --destroy skips both; --dry-run runs both
  - Completeness: every TaskTypeLiteral covered by _RESOURCE_CHECKERS or skip set,
    and by _OVERRIDE_CHECKERS; _ACCESS_DENIED_CODES ⊂ _CANNOT_VERIFY_CODES
"""

import typing
import pytest
from botocore.exceptions import BotoCoreError, ClientError

import polyris.resource_check as rc
from polyris.resource_check import (
    ResourceRef,
    ResourceCheckResult,
    ParamConflict,
    OverrideReport,
    OverrideCheckResult,
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
    _sanitize,
    _numeric_str,
    _fetch_glue_data,
    _fetch_ecs_task_def_data,
    _fetch_batch_job_def_data,
    _check_glue_override,
    _check_ecs_override,
    _check_batch_override,
    _OVERRIDE_CHECKERS,
    _OVERRIDE_INFO,
    _OVERRIDE_IAM_ACTION,
    check_resource_references,
    print_resource_check_result,
    check_resource_overrides,
    print_resource_override_result,
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
    # Glue override params
    t.worker_type = fields.get("worker_type", None)
    t.number_of_workers = fields.get("number_of_workers", None)
    t.max_capacity = fields.get("max_capacity", None)
    t.allocated_capacity = fields.get("allocated_capacity", None)
    t.glue_arguments = fields.get("glue_arguments", None)
    # ECS override params
    t.container_overrides = fields.get("container_overrides", None)
    # Batch override params
    t.batch_parameters = fields.get("batch_parameters", None)
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
        task = _make_task(
            mocker, task_type="lambda", arn="arn:aws:lambda:us-east-1:123:function:my-func", function_name="my-func"
        )
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
        task = _make_task(mocker, task_type="ecs", cluster="my-cluster", task_definition="my-task-def")
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
        task = _make_task(mocker, task_type="batch", job_queue="my-queue", job_definition="my-job-def")
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
        task = _make_task(mocker, task_type="glue", job_name="etl-job", role="arn:aws:iam::222222222222:role/etl")
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
        client.describe_task_definition.return_value = {"taskDefinition": {"status": "INACTIVE"}}
        with pytest.raises(_NotFoundError):
            _check_ecs_task_def(client, "old-task-def")

    def test_check_ecs_task_def_delete_in_progress_raises_not_found(self, mocker):
        client = mocker.MagicMock()
        client.describe_task_definition.return_value = {"taskDefinition": {"status": "DELETE_IN_PROGRESS"}}
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

    def test_check_batch_job_queue_creating_passes(self, mocker):
        """CREATING status is intentionally NOT in the unusable set — a queue in CREATING
        state accepts job submissions (they queue up). No false-positive missing error."""
        client = mocker.MagicMock()
        client.describe_job_queues.return_value = {"jobQueues": [{"jobQueueName": "q", "status": "CREATING"}]}
        _check_batch_job_queue(client, "q")  # no raise

    def test_check_batch_job_queue_updating_passes(self, mocker):
        """UPDATING status is intentionally NOT in the unusable set."""
        client = mocker.MagicMock()
        client.describe_job_queues.return_value = {"jobQueues": [{"jobQueueName": "q", "status": "UPDATING"}]}
        _check_batch_job_queue(client, "q")  # no raise

    def test_check_batch_job_def_found(self, mocker):
        client = mocker.MagicMock()
        client.describe_job_definitions.return_value = {
            "jobDefinitions": [{"jobDefinitionName": "jd", "status": "ACTIVE"}]
        }
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
        client.describe_job_definitions.assert_called_once_with(jobDefinitions=["inactive-def"], status="ACTIVE")

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
        task = _make_task(mocker, task_type="glue", job_name="etl", role="arn:aws:iam::222:role/etl")
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
        task = _make_task(mocker, task_type="sfn", arn="arn:aws:states:us-east-1:123:stateMachine:encrypted")
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
        tasks = [_make_task(mocker, task_type="glue", job_name="etl", task_id=f"t{i}") for i in range(5)]
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

        task = _make_task(mocker, task_type="sfn", arn="arn:aws:states:us-east-1:123:stateMachine:my-sfn")
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
        task = _make_task(mocker, task_type="ecs", cluster="ghost-cluster", task_definition="")
        result = check_resource_references(
            _make_dag(mocker, [task]),
            self._session(mocker, client),
            "us-east-1",
        )
        assert len(result.errors) == 1

    def test_batch_queue_empty_list_goes_to_errors(self, mocker):
        client = mocker.MagicMock()
        client.describe_job_queues.return_value = {"jobQueues": []}
        task = _make_task(mocker, task_type="batch", job_queue="ghost-queue", job_definition="")
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
            errors=[
                ResourceRef(task_id="process", kind="sfn", ref="arn:aws:states:us-east-1:123:stateMachine:missing")
            ],
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
            skipped=[
                ResourceRef(
                    task_id="cross_task",
                    kind="sfn",
                    ref="arn:aws:states:us-east-1:222:stateMachine:cross",
                    role="arn:aws:iam::222:role/etl",
                )
            ],
        )
        print_resource_check_result(result)
        out = capsys.readouterr().out
        assert "different IAM role" in out
        assert "arn:aws:iam::222:role/etl" in out
        assert "cross_task" in out

    def test_named_role_alias_prints_role_message(self, capsys, mocker):
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = ResourceCheckResult(
            skipped=[ResourceRef(task_id="my_task", kind="glue", ref="etl-job", role="acq")],
        )
        print_resource_check_result(result)
        out = capsys.readouterr().out
        assert "different IAM role" in out
        assert "acq" in out
        assert "my_task" in out

    def test_cross_account_truncation(self, capsys, mocker):
        mocker.patch("sys.stdout.isatty", return_value=False)
        skipped = [
            ResourceRef(task_id=f"t{i}", kind="glue", ref=f"job{i}", role="arn:aws:iam::222:role/etl")
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
            skipped=[ResourceRef(task_id="t3", kind="lambda", ref="fn", role="arn:aws:iam::222:role/etl")],
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
            skipped=[ResourceRef(task_id="t1", kind="glue", ref="job", role="arn:aws:iam::222:role/etl")],
        )
        print_resource_check_result(result)
        out = capsys.readouterr().out
        assert "aborting deploy" not in out
        assert "different IAM role" in out

    def test_task_id_with_newline_sanitized_in_output(self, capsys, mocker):
        """task_id containing \\n must not emit a bare newline in the printed line."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = ResourceCheckResult(
            errors=[ResourceRef(task_id="bad\ntask", kind="glue", ref="missing-job")],
            _checked=1,
        )
        print_resource_check_result(result)
        out = capsys.readouterr().out
        assert "bad\ntask" not in out  # \n replaced, not kept
        lines = out.splitlines()
        assert any("bad task" in line for line in lines)

    def test_task_id_with_newline_sanitized_in_warnings(self, capsys, mocker):
        """task_id \\n sanitized in the warnings block too."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        rr = ResourceRef(task_id="bad\ntask", kind="lambda", ref="my-func")
        result = ResourceCheckResult(warnings=[rr], _checked=1)
        print_resource_check_result(result)
        out = capsys.readouterr().out
        assert "could not be verified" in out  # confirms warnings block printed
        lines = out.splitlines()
        assert any("bad task" in line for line in lines)

    def test_task_id_with_newline_sanitized_in_skipped(self, capsys, mocker):
        """task_id \\n sanitized in the skipped block too."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = ResourceCheckResult(
            skipped=[ResourceRef(task_id="bad\ntask", kind="glue", ref="job", role="arn:aws:iam::222:role/etl")],
        )
        print_resource_check_result(result)
        out = capsys.readouterr().out
        lines = out.splitlines()
        assert any("bad task" in line for line in lines)

    def test_ref_with_newline_sanitized_in_errors(self, capsys, mocker):
        """rr.ref containing \\n must be sanitized in the errors block."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = ResourceCheckResult(
            errors=[ResourceRef(task_id="t1", kind="glue", ref="bad\nref")],
            _checked=1,
        )
        print_resource_check_result(result)
        out = capsys.readouterr().out
        lines = out.splitlines()
        assert any("bad ref" in line for line in lines)

    def test_ref_with_newline_sanitized_in_warnings(self, capsys, mocker):
        """rr.ref containing \\n must be sanitized in the warnings block."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        rr = ResourceRef(task_id="t1", kind="lambda", ref="bad\nref")
        result = ResourceCheckResult(warnings=[rr], _checked=1)
        print_resource_check_result(result)
        out = capsys.readouterr().out
        lines = out.splitlines()
        assert any("bad ref" in line for line in lines)

    def test_ref_with_newline_sanitized_in_skipped(self, capsys, mocker):
        """rr.ref containing \\n must be sanitized in the skipped block."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = ResourceCheckResult(
            skipped=[ResourceRef(task_id="t1", kind="glue", ref="bad\nref", role="arn:aws:iam::222:role/etl")],
        )
        print_resource_check_result(result)
        out = capsys.readouterr().out
        lines = out.splitlines()
        assert any("bad ref" in line for line in lines)

    def test_role_with_newline_sanitized_in_skipped(self, capsys, mocker):
        """rr.role containing \\n must be sanitized in the skipped block."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = ResourceCheckResult(
            skipped=[ResourceRef(task_id="t1", kind="glue", ref="job", role="bad\nrole")],
        )
        print_resource_check_result(result)
        out = capsys.readouterr().out
        lines = out.splitlines()
        assert any("bad role" in line for line in lines)

    def test_ref_with_ansi_codes_sanitized_in_errors(self, capsys, mocker):
        """ANSI escape codes in rr.ref must be stripped before printing."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = ResourceCheckResult(
            errors=[ResourceRef(task_id="t1", kind="glue", ref="\x1b[31mbad-ref\x1b[0m")],
            _checked=1,
        )
        print_resource_check_result(result)
        out = capsys.readouterr().out
        assert "\x1b[31m" not in out
        assert "bad-ref" in out


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
        "Stacks": [
            {
                "Outputs": [
                    {"OutputKey": "DependencyWrapperArn", "OutputValue": "arn:wrapper"},
                    {"OutputKey": "OrchestrationRoleArn", "OutputValue": "arn:role"},
                    {"OutputKey": "PipelineRegistryTable", "OutputValue": "reg"},
                    {"OutputKey": "PipelineTokensTable", "OutputValue": "tok"},
                    {"OutputKey": "AssetSubscriptionsTable", "OutputValue": "asset"},
                ],
                "Parameters": [],
            }
        ]
    }
    mocker.patch("polyris.deploy.boto3").Session.return_value = session

    mocker.patch(
        "polyris.deploy.check_resource_references",
        return_value=resource_result,
    )
    mocker.patch("polyris.deploy.print_resource_check_result")
    mocker.patch(
        "polyris.deploy.check_resource_overrides",
        return_value=OverrideCheckResult(),
    )
    mocker.patch("polyris.deploy.print_resource_override_result")

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
    deploy.check_resource_overrides.assert_called_once()


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


def test_deploy_pipeline_warnings_no_errors_still_runs_override_check(mocker):
    """Existence check with warnings (but no errors) must still invoke the override check."""
    result = ResourceCheckResult(
        warnings=[ResourceRef(task_id="t1", kind="glue", ref="etl")],
        _checked=1,
    )
    dag = _make_deploy_mocks(mocker, resource_result=result)
    with pytest.raises(SystemExit) as exc:
        from polyris import deploy

        deploy.deploy_pipeline(dag)
    assert exc.value.code == 2  # hit _generate_cfn_template barrier, not a resource failure
    deploy.check_resource_overrides.assert_called_once()


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
        "Stacks": [
            {
                "Outputs": [
                    {"OutputKey": "DependencyWrapperArn", "OutputValue": "arn:wrapper"},
                    {"OutputKey": "OrchestrationRoleArn", "OutputValue": "arn:role"},
                ],
                "Parameters": [],
            }
        ]
    }

    def _client(name, **kw):
        return {"sts": sts, "cloudformation": cfn}.get(name, mocker.MagicMock())

    session = mocker.MagicMock()
    session.client.side_effect = _client
    mocker.patch("polyris.deploy.boto3").Session.return_value = session

    rc_patch = mocker.patch("polyris.deploy.check_resource_references")
    ro_patch = mocker.patch("polyris.deploy.check_resource_overrides")

    from polyris import deploy

    deploy.deploy_pipeline(dag, destroy=True, dry_run=True)

    rc_patch.assert_not_called()
    ro_patch.assert_not_called()


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


# ---------------------------------------------------------------------------
# _failed_refs: populated correctly by check_resource_references
# ---------------------------------------------------------------------------


class TestFailedRefs:
    def _session(self, mocker, client_mock=None):
        session = mocker.MagicMock()
        if client_mock is not None:
            session.client.return_value = client_mock
        return session

    def test_not_found_adds_to_failed_refs(self, mocker):
        client = mocker.MagicMock()
        client.get_job.side_effect = _client_error("EntityNotFoundException")
        task = _make_task(mocker, task_type="glue", job_name="missing-job")
        result = check_resource_references(_make_dag(mocker, [task]), self._session(mocker, client), "us-east-1")
        assert ("glue", "missing-job") in result.failed_refs

    def test_access_denied_adds_to_failed_refs(self, mocker):
        client = mocker.MagicMock()
        client.get_job.side_effect = _client_error("AccessDeniedException")
        task = _make_task(mocker, task_type="glue", job_name="my-job")
        result = check_resource_references(_make_dag(mocker, [task]), self._session(mocker, client), "us-east-1")
        assert ("glue", "my-job") in result.failed_refs

    def test_throttle_does_not_add_to_failed_refs(self, mocker):
        """Throttle is transient — the resource may exist; override check should still attempt it."""
        client = mocker.MagicMock()
        client.get_job.side_effect = _client_error("ThrottlingException")
        task = _make_task(mocker, task_type="glue", job_name="my-job")
        result = check_resource_references(_make_dag(mocker, [task]), self._session(mocker, client), "us-east-1")
        assert ("glue", "my-job") not in result.failed_refs

    def test_botocore_error_does_not_add_to_failed_refs(self, mocker):
        client = mocker.MagicMock()
        client.get_job.side_effect = BotoCoreError()
        task = _make_task(mocker, task_type="glue", job_name="my-job")
        result = check_resource_references(_make_dag(mocker, [task]), self._session(mocker, client), "us-east-1")
        assert ("glue", "my-job") not in result.failed_refs

    def test_unknown_client_error_adds_to_failed_refs(self, mocker):
        client = mocker.MagicMock()
        client.get_job.side_effect = _client_error("SomeOtherException")
        task = _make_task(mocker, task_type="glue", job_name="my-job")
        result = check_resource_references(_make_dag(mocker, [task]), self._session(mocker, client), "us-east-1")
        assert ("glue", "my-job") in result.failed_refs

    def test_found_resource_not_in_failed_refs(self, mocker):
        client = mocker.MagicMock()
        client.get_job.return_value = {"Job": {"Name": "etl"}}
        task = _make_task(mocker, task_type="glue", job_name="etl")
        result = check_resource_references(_make_dag(mocker, [task]), self._session(mocker, client), "us-east-1")
        assert result.failed_refs == set()

    def test_ecs_cluster_error_uses_cluster_kind(self, mocker):
        client = mocker.MagicMock()
        client.describe_clusters.return_value = {"clusters": []}
        task = _make_task(mocker, task_type="ecs", cluster="bad-cluster", task_definition="")
        result = check_resource_references(_make_dag(mocker, [task]), self._session(mocker, client), "us-east-1")
        assert ("ecs_cluster", "bad-cluster") in result.failed_refs

    def test_ecs_task_def_error_uses_ecs_task_definition_kind(self, mocker):
        """ECS task def inactive → kind is 'ecs_task_definition' so override check skips it."""
        client = mocker.MagicMock()
        client.describe_task_definition.return_value = {"taskDefinition": {"status": "INACTIVE"}}
        task = _make_task(mocker, task_type="ecs", cluster="", task_definition="my-task-def")
        result = check_resource_references(_make_dag(mocker, [task]), self._session(mocker, client), "us-east-1")
        assert ("ecs_task_definition", "my-task-def") in result.failed_refs


# ---------------------------------------------------------------------------
# _numeric_str helper
# ---------------------------------------------------------------------------


class TestNumericStr:
    def test_numeric_str_int(self):
        assert _numeric_str(1024) is True

    def test_numeric_str_float(self):
        assert _numeric_str(1.5) is True

    def test_numeric_str_digit_string(self):
        assert _numeric_str("512") is True

    def test_numeric_str_non_digit_string(self):
        assert _numeric_str("auto") is False

    def test_numeric_str_none(self):
        assert _numeric_str(None) is False

    def test_numeric_str_nan_float_rejected(self):
        """float("nan") passes float() but int(float("nan")) raises — must be rejected."""
        assert _numeric_str(float("nan")) is False

    def test_numeric_str_inf_float_rejected(self):
        """float("inf") causes OverflowError in int() — must be rejected."""
        assert _numeric_str(float("inf")) is False

    def test_numeric_str_nan_string_rejected(self):
        assert _numeric_str("nan") is False

    def test_numeric_str_inf_string_rejected(self):
        assert _numeric_str("inf") is False

    def test_numeric_str_true_rejected(self):
        """`isinstance(True, int) is True` in Python — bool must be rejected
        explicitly, otherwise `container_overrides={"cpu": True}` would coerce
        to `1` and produce `differs: declared=True, actual=1024`."""
        assert _numeric_str(True) is False

    def test_numeric_str_false_rejected(self):
        assert _numeric_str(False) is False


# ---------------------------------------------------------------------------
# _sanitize helper
# ---------------------------------------------------------------------------


class TestSanitize:
    def test_strips_newlines(self):
        """Newlines are replaced with a space, not removed."""
        assert _sanitize("line1\nline2") == "line1 line2"

    def test_strips_carriage_returns(self):
        """Carriage returns are replaced with a space, not removed."""
        assert _sanitize("line1\rline2") == "line1 line2"

    def test_converts_non_strings(self):
        """Non-string inputs are coerced via str() before processing."""
        assert _sanitize(42) == "42"
        assert _sanitize(3.14) == "3.14"

    def test_long_string_truncated_with_ellipsis(self):
        """Strings over 200 chars: truncated to 197 chars + '...' = 200 total."""
        long = "x" * 250
        result = _sanitize(long)
        assert len(result) == 200
        assert result.endswith("...")
        assert result == "x" * 197 + "..."

    def test_exactly_200_chars_not_truncated(self):
        """200-char string is at the limit and must not be truncated."""
        s = "y" * 200
        assert _sanitize(s) == s

    def test_short_string_unchanged(self):
        """Plain ASCII strings under the limit pass through unmodified."""
        assert _sanitize("hello") == "hello"

    def test_strips_ansi_color_codes(self):
        """ANSI SGR escape sequences must be stripped before terminal output."""
        s = "\x1b[31mred\x1b[0m"
        assert _sanitize(s) == "red"

    def test_strips_ansi_bold_code(self):
        """Multi-param CSI sequences (e.g. bold+color) are also stripped."""
        s = "\x1b[1;32mbold green\x1b[0m"
        assert _sanitize(s) == "bold green"

    def test_strips_tab(self):
        """Tab characters must be replaced with a space."""
        assert _sanitize("a\tb") == "a b"

    def test_ansi_plus_newline_both_stripped(self):
        """CSI ANSI escape codes stripped and newlines replaced with space in one pass."""
        s = "\x1b[31mred\x1b[0m\nand more"
        assert _sanitize(s) == "red and more"

    def test_ansi_does_not_count_toward_200_char_limit(self):
        """ANSI codes are stripped before truncation — they don't consume display space."""
        # 10 visible chars + lots of ANSI padding — should not be truncated
        s = "\x1b[31m" + "x" * 10 + "\x1b[0m"
        result = _sanitize(s)
        assert result == "x" * 10


# ---------------------------------------------------------------------------
# Data fetcher functions
# ---------------------------------------------------------------------------


class TestFetchers:
    def test_fetch_glue_data_returns_job(self, mocker):
        client = mocker.MagicMock()
        client.get_job.return_value = {"Job": {"Name": "etl", "WorkerType": "G.1X"}}
        result = _fetch_glue_data(client, "etl")
        client.get_job.assert_called_once_with(JobName="etl")
        assert result["WorkerType"] == "G.1X"

    def test_fetch_glue_data_missing_job_key_raises_not_found(self, mocker):
        """Malformed AWS response (no 'Job' key) must raise _NotFoundError, not return {}
        — prevents a false-clean report where _verified increments but nothing was compared."""
        client = mocker.MagicMock()
        client.get_job.return_value = {}
        with pytest.raises(_NotFoundError):
            _fetch_glue_data(client, "etl")

    def test_fetch_ecs_task_def_data_returns_task_definition(self, mocker):
        client = mocker.MagicMock()
        client.describe_task_definition.return_value = {"taskDefinition": {"containerDefinitions": [{"name": "app"}]}}
        result = _fetch_ecs_task_def_data(client, "my-def")
        client.describe_task_definition.assert_called_once_with(taskDefinition="my-def")
        assert result["containerDefinitions"][0]["name"] == "app"

    def test_fetch_ecs_task_def_data_missing_key_raises_not_found(self, mocker):
        """Malformed AWS response (no 'taskDefinition' key) must raise _NotFoundError."""
        client = mocker.MagicMock()
        client.describe_task_definition.return_value = {}
        with pytest.raises(_NotFoundError):
            _fetch_ecs_task_def_data(client, "my-def")

    def test_fetch_batch_job_def_data_returns_highest_revision(self, mocker):
        client = mocker.MagicMock()
        client.describe_job_definitions.return_value = {
            "jobDefinitions": [
                {"jobDefinitionName": "jd", "revision": 2, "parameters": {"k": "v2"}},
                {"jobDefinitionName": "jd", "revision": 5, "parameters": {"k": "v5"}},
                {"jobDefinitionName": "jd", "revision": 3, "parameters": {"k": "v3"}},
            ]
        }
        result = _fetch_batch_job_def_data(client, "jd")
        assert result["parameters"]["k"] == "v5"

    def test_fetch_batch_job_def_data_empty_raises_not_found(self, mocker):
        """Empty ACTIVE definition list → _NotFoundError; returning {} would produce
        a false-clean override report for a resource that no longer exists."""
        client = mocker.MagicMock()
        client.describe_job_definitions.return_value = {"jobDefinitions": []}
        with pytest.raises(_NotFoundError):
            _fetch_batch_job_def_data(client, "jd")

    def test_fetch_batch_job_def_data_single_no_revision(self, mocker):
        """Single definition without 'revision' key: defaults to 0 via dict.get, still selected."""
        client = mocker.MagicMock()
        client.describe_job_definitions.return_value = {
            "jobDefinitions": [{"jobDefinitionName": "jd", "parameters": {"k": "v"}}]
        }
        result = _fetch_batch_job_def_data(client, "jd")
        assert result["parameters"]["k"] == "v"


# ---------------------------------------------------------------------------
# _check_glue_override
# ---------------------------------------------------------------------------


class TestCheckGlueOverride:
    def _task(self, mocker, **kw):
        return _make_task(mocker, task_type="glue", task_id="ingest", job_name="etl", **kw)

    def test_no_override_params_returns_none(self, mocker):
        task = self._task(mocker)
        assert _check_glue_override(task, {}) is None

    def test_worker_type_matches_returns_matching(self, mocker):
        task = self._task(mocker, worker_type="G.1X")
        report = _check_glue_override(task, {"WorkerType": "G.1X"})
        assert report is not None
        assert any("WorkerType" in m for m in report.matching)
        assert report.differs == []

    def test_worker_type_differs_returns_conflict(self, mocker):
        task = self._task(mocker, worker_type="G.2X")
        report = _check_glue_override(task, {"WorkerType": "G.1X"})
        assert report is not None
        assert len(report.differs) == 1
        conflict = report.differs[0]
        assert conflict.name == "WorkerType"
        assert conflict.declared == "G.2X"
        assert conflict.actual == "G.1X"

    def test_worker_type_absent_in_job_skipped(self, mocker):
        """If AWS job has no WorkerType (Python Shell), declared is not a conflict."""
        task = self._task(mocker, worker_type="G.1X")
        assert _check_glue_override(task, {}) is None

    def test_number_of_workers_matches(self, mocker):
        task = self._task(mocker, number_of_workers=5)
        report = _check_glue_override(task, {"NumberOfWorkers": 5})
        assert report is not None
        assert any("NumberOfWorkers" in m for m in report.matching)

    def test_number_of_workers_differs(self, mocker):
        task = self._task(mocker, number_of_workers=10)
        report = _check_glue_override(task, {"NumberOfWorkers": 5})
        assert report is not None
        assert any(c.name == "NumberOfWorkers" for c in report.differs)

    def test_max_capacity_matches(self, mocker):
        task = self._task(mocker, max_capacity=1.0)
        report = _check_glue_override(task, {"MaxCapacity": 1.0})
        assert report is not None
        assert any("MaxCapacity" in m for m in report.matching)

    def test_allocated_capacity_differs(self, mocker):
        task = self._task(mocker, allocated_capacity=5)
        report = _check_glue_override(task, {"AllocatedCapacity": 2})
        assert report is not None
        assert any(c.name == "AllocatedCapacity" for c in report.differs)

    def test_glue_argument_matches_default(self, mocker):
        task = self._task(mocker, glue_arguments={"--output": "s3://bucket/out"})
        job = {"DefaultArguments": {"--output": "s3://bucket/out"}}
        report = _check_glue_override(task, job)
        assert report is not None
        assert any("--output" in m for m in report.matching)

    def test_glue_argument_differs_from_default(self, mocker):
        task = self._task(mocker, glue_arguments={"--output": "s3://bucket/new"})
        job = {"DefaultArguments": {"--output": "s3://bucket/old"}}
        report = _check_glue_override(task, job)
        assert report is not None
        assert any(c.name == "--output" for c in report.differs)

    def test_glue_argument_not_in_defaults_not_reported(self, mocker):
        """Arg not in DefaultArguments is a new addition — not a conflict."""
        task = self._task(mocker, glue_arguments={"--new-arg": "value"})
        job = {"DefaultArguments": {}}
        assert _check_glue_override(task, job) is None

    def test_non_overridable_argument_reported_as_silent_drop(self, mocker):
        task = self._task(mocker, glue_arguments={"--locked": "val"})
        job = {
            "NonOverridableArguments": {"--locked": "fixed"},
            "DefaultArguments": {"--locked": "fixed"},
        }
        report = _check_glue_override(task, job)
        assert report is not None
        assert "--locked" in report.silent_ignores

    def test_multiple_silent_ignores(self, mocker):
        task = self._task(mocker, glue_arguments={"--locked-a": "v1", "--locked-b": "v2"})
        job = {
            "NonOverridableArguments": {"--locked-a": "x", "--locked-b": "y"},
            "DefaultArguments": {},
        }
        report = _check_glue_override(task, job)
        assert report is not None
        assert len(report.silent_ignores) == 2
        assert "--locked-a" in report.silent_ignores
        assert "--locked-b" in report.silent_ignores

    def test_combined_differs_silent_and_matching(self, mocker):
        task = self._task(
            mocker,
            worker_type="G.2X",
            glue_arguments={
                "--locked": "x",
                "--output": "s3://bucket/out",
                "--format": "json",
            },
        )
        job = {
            "WorkerType": "G.1X",
            "NonOverridableArguments": {"--locked": "fixed"},
            "DefaultArguments": {"--output": "s3://bucket/out", "--format": "parquet"},
        }
        report = _check_glue_override(task, job)
        assert report is not None
        assert any(c.name == "WorkerType" for c in report.differs)
        assert any(c.name == "--format" for c in report.differs)
        assert "--locked" in report.silent_ignores
        assert any("--output" in m for m in report.matching)

    def test_report_metadata(self, mocker):
        task = self._task(mocker, worker_type="G.2X")
        report = _check_glue_override(task, {"WorkerType": "G.1X"})
        assert report is not None
        assert report.task_id == "ingest"
        assert report.service == "glue"
        assert report.resource_name == "etl"

    def test_worker_model_skips_capacity_fields(self, mocker):
        """When worker_type is declared (no max_capacity), capacity fields are absent
        on the task and therefore skipped — they cannot appear in differs."""
        task = self._task(mocker, worker_type="G.2X")
        job = {"WorkerType": "G.2X", "MaxCapacity": 8.0, "AllocatedCapacity": 4}
        report = _check_glue_override(task, job)
        # worker_type matches → matching; capacity fields undeclared → not in differs
        assert report is not None
        assert not any(c.name in ("MaxCapacity", "AllocatedCapacity") for c in report.differs)

    def test_capacity_model_skips_worker_fields(self, mocker):
        """When max_capacity is declared (no worker_type), worker fields are absent
        on the task and therefore skipped — they cannot appear in differs."""
        task = self._task(mocker, max_capacity=4.0)
        job = {"MaxCapacity": 4.0, "WorkerType": "G.1X", "NumberOfWorkers": 2}
        report = _check_glue_override(task, job)
        # max_capacity matches → matching; worker fields undeclared → not in differs
        assert report is not None
        assert not any(c.name in ("WorkerType", "NumberOfWorkers") for c in report.differs)

    def test_both_models_declared_compares_both(self, mocker):
        """When both worker_type and max_capacity are declared, both are compared
        against the AWS job independently."""
        task = self._task(mocker, worker_type="G.1X", max_capacity=5.0)
        job = {"WorkerType": "G.1X", "MaxCapacity": 8.0}
        report = _check_glue_override(task, job)
        # worker_type matches → not in differs; max_capacity differs → in differs
        assert report is not None
        assert not any(c.name == "WorkerType" for c in report.differs)
        assert any(c.name == "MaxCapacity" for c in report.differs)


# ---------------------------------------------------------------------------
# _check_ecs_override
# ---------------------------------------------------------------------------


class TestCheckEcsOverride:
    def _task(self, mocker, **kw):
        return _make_task(mocker, task_type="ecs", task_id="run_job", task_definition="my-task-def", **kw)

    def test_no_container_overrides_returns_none(self, mocker):
        task = self._task(mocker)
        assert _check_ecs_override(task, {}) is None

    def test_empty_container_overrides_returns_none(self, mocker):
        task = self._task(mocker, container_overrides={})
        assert _check_ecs_override(task, {}) is None

    def test_command_differs(self, mocker):
        task = self._task(
            mocker, container_overrides={"containerOverrides": [{"name": "app", "command": ["python", "run.py"]}]}
        )
        task_def = {"containerDefinitions": [{"name": "app", "command": ["python", "old.py"]}]}
        report = _check_ecs_override(task, task_def)
        assert report is not None
        assert any(c.name == "command" for c in report.differs)

    def test_command_matches(self, mocker):
        cmd = ["python", "run.py"]
        task = self._task(mocker, container_overrides={"containerOverrides": [{"name": "app", "command": cmd}]})
        task_def = {"containerDefinitions": [{"name": "app", "command": cmd}]}
        report = _check_ecs_override(task, task_def)
        assert report is not None
        assert any("command" in m for m in report.matching)

    def test_cpu_differs(self, mocker):
        task = self._task(mocker, container_overrides={"containerOverrides": [{"name": "app", "cpu": 2048}]})
        task_def = {"containerDefinitions": [{"name": "app", "cpu": 1024}]}
        report = _check_ecs_override(task, task_def)
        assert report is not None
        assert any(c.name == "cpu" for c in report.differs)

    def test_memory_matches(self, mocker):
        task = self._task(mocker, container_overrides={"containerOverrides": [{"name": "app", "memory": 512}]})
        task_def = {"containerDefinitions": [{"name": "app", "memory": 512}]}
        report = _check_ecs_override(task, task_def)
        assert report is not None
        assert any("memory" in m for m in report.matching)

    def test_cpu_string_input_stored_as_normalized_int(self, mocker):
        """cpu declared as digit-string is normalized to int in ParamConflict.declared."""
        task = self._task(
            mocker,
            container_overrides={
                "containerOverrides": [{"name": "app", "cpu": "2048"}]  # string from JSON
            },
        )
        task_def = {"containerDefinitions": [{"name": "app", "cpu": 1024}]}
        report = _check_ecs_override(task, task_def)
        assert report is not None
        conflict = next(c for c in report.differs if c.name == "cpu")
        assert conflict.declared == 2048  # normalized to int, not "2048"
        assert conflict.actual == 1024

    def test_cpu_non_numeric_skipped(self, mocker):
        """Non-numeric cpu value cannot be compared — no conflict reported."""
        task = self._task(mocker, container_overrides={"containerOverrides": [{"name": "app", "cpu": "auto"}]})
        task_def = {"containerDefinitions": [{"name": "app", "cpu": 1024}]}
        assert _check_ecs_override(task, task_def) is None

    def test_env_var_differs(self, mocker):
        task = self._task(
            mocker,
            container_overrides={
                "containerOverrides": [{"name": "app", "environment": [{"name": "LOG_LEVEL", "value": "DEBUG"}]}]
            },
        )
        task_def = {"containerDefinitions": [{"name": "app", "environment": [{"name": "LOG_LEVEL", "value": "INFO"}]}]}
        report = _check_ecs_override(task, task_def)
        assert report is not None
        assert any(c.name == "env:LOG_LEVEL" for c in report.differs)

    def test_env_var_matches(self, mocker):
        task = self._task(
            mocker,
            container_overrides={
                "containerOverrides": [{"name": "app", "environment": [{"name": "STAGE", "value": "prod"}]}]
            },
        )
        task_def = {"containerDefinitions": [{"name": "app", "environment": [{"name": "STAGE", "value": "prod"}]}]}
        report = _check_ecs_override(task, task_def)
        assert report is not None
        assert any("env:STAGE" in m for m in report.matching)

    def test_env_var_not_in_base_container_not_reported(self, mocker):
        """New env var not set in task def is an addition, not a conflict."""
        task = self._task(
            mocker,
            container_overrides={
                "containerOverrides": [{"name": "app", "environment": [{"name": "NEW_VAR", "value": "x"}]}]
            },
        )
        task_def = {"containerDefinitions": [{"name": "app", "environment": []}]}
        assert _check_ecs_override(task, task_def) is None

    def test_env_var_with_no_name_skipped(self, mocker):
        """Environment entry without a 'name' key must not crash or report a conflict."""
        task = self._task(
            mocker,
            container_overrides={
                "containerOverrides": [
                    {
                        "name": "app",
                        "environment": [
                            {"value": "orphan-value"},  # no "name" key → skip
                        ],
                    }
                ]
            },
        )
        task_def = {"containerDefinitions": [{"name": "app"}]}
        assert _check_ecs_override(task, task_def) is None

    def test_duplicate_container_names_first_wins(self, mocker):
        """Duplicate container names in task def: first entry used for comparison."""
        task = self._task(mocker, container_overrides={"containerOverrides": [{"name": "app", "cpu": 2048}]})
        task_def = {
            "containerDefinitions": [
                {"name": "app", "cpu": 1024},  # first
                {"name": "app", "cpu": 512},  # duplicate — ignored
            ]
        }
        report = _check_ecs_override(task, task_def)
        assert report is not None
        assert report.differs[0].actual == 1024  # first entry used

    def test_report_metadata(self, mocker):
        task = self._task(mocker, container_overrides={"containerOverrides": [{"name": "app", "cpu": 2048}]})
        task_def = {"containerDefinitions": [{"name": "app", "cpu": 1024}]}
        report = _check_ecs_override(task, task_def)
        assert report is not None
        assert report.task_id == "run_job"
        assert report.service == "ecs"
        assert report.resource_name == "my-task-def"


# ---------------------------------------------------------------------------
# _check_batch_override
# ---------------------------------------------------------------------------


class TestCheckBatchOverride:
    def _task(self, mocker, **kw):
        return _make_task(mocker, task_type="batch", task_id="process", job_definition="my-job-def", **kw)

    def test_no_batch_parameters_returns_none(self, mocker):
        task = self._task(mocker)
        assert _check_batch_override(task, {}) is None

    def test_parameter_matches_default(self, mocker):
        task = self._task(mocker, batch_parameters={"format": "parquet"})
        report = _check_batch_override(task, {"parameters": {"format": "parquet"}})
        assert report is not None
        assert any("format" in m for m in report.matching)

    def test_parameter_differs_from_default(self, mocker):
        task = self._task(mocker, batch_parameters={"format": "json"})
        report = _check_batch_override(task, {"parameters": {"format": "parquet"}})
        assert report is not None
        assert any(c.name == "format" for c in report.differs)
        conflict = next(c for c in report.differs if c.name == "format")
        assert conflict.declared == "json"
        assert conflict.actual == "parquet"

    def test_parameter_int_value_coerced_to_string(self, mocker):
        """Non-string batch_parameters values are str()-coerced before comparison.
        AWS Batch parameters are always strings — an integer 100 matches default '100'."""
        task = self._task(mocker, batch_parameters={"count": 100})  # int, not str
        report = _check_batch_override(task, {"parameters": {"count": "100"}})
        assert report is not None
        assert any("count" in m for m in report.matching)  # match, not conflict

    def test_parameter_int_value_conflict_stores_string(self, mocker):
        """Conflicting int value is stored as string in ParamConflict."""
        task = self._task(mocker, batch_parameters={"count": 200})
        report = _check_batch_override(task, {"parameters": {"count": "100"}})
        assert report is not None
        conflict = next(c for c in report.differs if c.name == "count")
        assert conflict.declared == "200"
        assert conflict.actual == "100"

    def test_parameter_not_in_defaults_not_reported(self, mocker):
        """A new param not in job def defaults is an addition, not a conflict."""
        task = self._task(mocker, batch_parameters={"new-param": "val"})
        assert _check_batch_override(task, {"parameters": {}}) is None

    def test_empty_job_def_parameters_no_conflicts(self, mocker):
        task = self._task(mocker, batch_parameters={"k": "v"})
        assert _check_batch_override(task, {}) is None

    def test_report_metadata(self, mocker):
        task = self._task(mocker, batch_parameters={"k": "new"})
        report = _check_batch_override(task, {"parameters": {"k": "old"}})
        assert report is not None
        assert report.task_id == "process"
        assert report.service == "batch"
        assert report.resource_name == "my-job-def"


# ---------------------------------------------------------------------------
# check_resource_overrides (orchestrator)
# ---------------------------------------------------------------------------


class TestCheckResourceOverrides:
    def _session(self, mocker, client_mock=None):
        session = mocker.MagicMock()
        if client_mock is not None:
            session.client.return_value = client_mock
        return session

    def test_empty_dag_returns_empty_result(self, mocker):
        result = check_resource_overrides(_make_dag(mocker, []), self._session(mocker), "us-east-1")
        assert result.reports == []
        assert result.unverified == []
        assert result._verified == 0

    def test_non_override_task_types_skipped(self, mocker):
        """sfn, lambda, athena, emr, sagemaker have no override params — skipped."""
        client = mocker.MagicMock()
        tasks = [
            _make_task(mocker, task_type="sfn", task_id="t1", arn="arn:aws:states:us-east-1:123:stateMachine:x"),
            _make_task(mocker, task_type="athena", task_id="t2", workgroup="primary"),
        ]
        result = check_resource_overrides(_make_dag(mocker, tasks), self._session(mocker, client), "us-east-1")
        assert result.reports == []
        assert result._verified == 0
        client.get_job.assert_not_called()

    def test_cross_account_task_skipped(self, mocker):
        client = mocker.MagicMock()
        task = _make_task(
            mocker, task_type="glue", job_name="etl", worker_type="G.2X", role="arn:aws:iam::222:role/etl"
        )
        result = check_resource_overrides(_make_dag(mocker, [task]), self._session(mocker, client), "us-east-1")
        assert result.reports == []
        client.get_job.assert_not_called()

    def test_cfn_substitution_ref_skipped(self, mocker):
        client = mocker.MagicMock()
        task = _make_task(mocker, task_type="glue", job_name="${JobName}", worker_type="G.2X")
        result = check_resource_overrides(_make_dag(mocker, [task]), self._session(mocker, client), "us-east-1")
        assert result.reports == []
        client.get_job.assert_not_called()

    def test_empty_ref_skipped(self, mocker):
        client = mocker.MagicMock()
        task = _make_task(mocker, task_type="glue", job_name="")
        result = check_resource_overrides(_make_dag(mocker, [task]), self._session(mocker, client), "us-east-1")
        assert result.reports == []
        client.get_job.assert_not_called()

    def test_failed_ref_skipped(self, mocker):
        """Resources in failed_refs (errors + access_denied from existence check) are skipped."""
        client = mocker.MagicMock()
        task = _make_task(mocker, task_type="glue", job_name="etl", worker_type="G.2X")
        result = check_resource_overrides(
            _make_dag(mocker, [task]),
            self._session(mocker, client),
            "us-east-1",
            failed_refs={("glue", "etl")},
        )
        assert result.reports == []
        assert result._skipped == 1
        client.get_job.assert_not_called()

    def test_glue_conflict_reported(self, mocker):
        client = mocker.MagicMock()
        client.get_job.return_value = {"Job": {"WorkerType": "G.1X"}}
        task = _make_task(mocker, task_type="glue", job_name="etl", worker_type="G.2X")
        result = check_resource_overrides(_make_dag(mocker, [task]), self._session(mocker, client), "us-east-1")
        assert len(result.reports) == 1
        assert result.reports[0].service == "glue"
        assert result._verified == 1

    def test_api_call_deduplicated_for_same_ref(self, mocker):
        """Two tasks referencing the same Glue job → one API call, two reports."""
        client = mocker.MagicMock()
        client.get_job.return_value = {"Job": {"WorkerType": "G.2X"}}
        tasks = [
            _make_task(
                mocker, task_type="glue", job_name="etl", worker_type="G.2X", task_id="t1"
            ),  # matches → matching report
            _make_task(
                mocker, task_type="glue", job_name="etl", worker_type="G.1X", task_id="t2"
            ),  # conflicts → differs report
        ]
        result = check_resource_overrides(_make_dag(mocker, tasks), self._session(mocker, client), "us-east-1")
        assert client.get_job.call_count == 1  # deduped: one API call for both tasks
        assert result._verified == 1  # one unique resource fetched
        assert len(result.reports) == 2  # one report per task (t1 matching, t2 differs)

    def test_access_denied_adds_to_unverified(self, mocker):
        client = mocker.MagicMock()
        client.get_job.side_effect = _client_error("AccessDeniedException")
        task = _make_task(mocker, task_type="glue", job_name="etl", worker_type="G.2X")
        result = check_resource_overrides(_make_dag(mocker, [task]), self._session(mocker, client), "us-east-1")
        assert len(result.unverified) == 1
        assert result.reports == []
        assert result._verified == 0

    def test_throttle_adds_to_unverified(self, mocker):
        client = mocker.MagicMock()
        client.get_job.side_effect = _client_error("ThrottlingException")
        task = _make_task(mocker, task_type="glue", job_name="etl", worker_type="G.2X")
        result = check_resource_overrides(_make_dag(mocker, [task]), self._session(mocker, client), "us-east-1")
        assert len(result.unverified) == 1

    def test_botocore_error_adds_to_unverified(self, mocker):
        client = mocker.MagicMock()
        client.get_job.side_effect = BotoCoreError()
        task = _make_task(mocker, task_type="glue", job_name="etl", worker_type="G.2X")
        result = check_resource_overrides(_make_dag(mocker, [task]), self._session(mocker, client), "us-east-1")
        assert len(result.unverified) == 1
        assert result._verified == 0

    def test_expired_token_short_circuits_remaining(self, mocker):
        """ExpiredTokenException on first call → all subsequent resources go to unverified."""
        client = mocker.MagicMock()
        client.get_job.side_effect = [
            _client_error("ExpiredTokenException"),
            {"Job": {"WorkerType": "G.1X"}},  # would succeed but should not be called
        ]
        tasks = [
            _make_task(mocker, task_type="glue", job_name="etl-a", worker_type="G.2X", task_id="t1"),
            _make_task(mocker, task_type="glue", job_name="etl-b", worker_type="G.2X", task_id="t2"),
        ]
        result = check_resource_overrides(_make_dag(mocker, tasks), self._session(mocker, client), "us-east-1")
        assert client.get_job.call_count == 1  # second call not made
        assert len(result.unverified) == 2

    def test_unverified_deduped_for_same_ref(self, mocker):
        """Two tasks failing on the same resource → one unverified entry."""
        client = mocker.MagicMock()
        client.get_job.side_effect = _client_error("AccessDeniedException")
        tasks = [
            _make_task(mocker, task_type="glue", job_name="etl", worker_type="G.2X", task_id="t1"),
            _make_task(mocker, task_type="glue", job_name="etl", worker_type="G.1X", task_id="t2"),
        ]
        result = check_resource_overrides(_make_dag(mocker, tasks), self._session(mocker, client), "us-east-1")
        assert len(result.unverified) == 1

    def test_no_failed_refs_defaults_to_empty_set(self, mocker):
        """failed_refs=None is treated as empty — no skip-on-existence-failure."""
        client = mocker.MagicMock()
        client.get_job.return_value = {"Job": {"WorkerType": "G.1X"}}
        task = _make_task(mocker, task_type="glue", job_name="etl")
        result = check_resource_overrides(_make_dag(mocker, [task]), self._session(mocker, client), "us-east-1")
        assert result._verified == 1

    def test_arn_ref_uses_arns_region(self, mocker):
        """ECS task def ARN pointing to us-west-2 must query us-west-2, not the deploy region."""
        session = mocker.MagicMock()
        client = mocker.MagicMock()
        client.describe_task_definition.return_value = {"taskDefinition": {"containerDefinitions": []}}
        session.client.return_value = client
        task = _make_task(
            mocker,
            task_type="ecs",
            task_definition="arn:aws:ecs:us-west-2:123:task-definition/my-def:5",
            container_overrides={"containerOverrides": []},
        )
        check_resource_overrides(_make_dag(mocker, [task]), session, "eu-west-1")
        session.client.assert_called_once_with("ecs", region_name="us-west-2")


# ---------------------------------------------------------------------------
# print_resource_override_result (output format)
# ---------------------------------------------------------------------------


class TestPrintResourceOverrideResult:
    def test_empty_result_prints_nothing(self, capsys):
        print_resource_override_result(OverrideCheckResult())
        assert capsys.readouterr().out == ""

    def test_success_message_when_checked_and_no_issues(self, capsys, mocker):
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(_verified=3)
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert "✓" in out
        assert "no issues" in out

    def test_success_message_singular_noun(self, capsys, mocker):
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(_verified=1)
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert "1 override resource" in out
        assert "resources" not in out

    def test_no_success_message_when_zero_checked(self, capsys):
        result = OverrideCheckResult(_verified=0)
        print_resource_override_result(result)
        assert capsys.readouterr().out == ""

    def test_success_note_includes_skipped_count(self, capsys, mocker):
        """Success tick must include '(N not re-checked — already reported in existence check above)' when _skipped > 0."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(_verified=2, _skipped=1)
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert "✓" in out
        assert "no issues" in out
        assert "1 skipped" in out
        assert "already reported in existence check above" in out

    def test_success_no_skip_note_when_skipped_zero(self, capsys, mocker):
        """Success tick must NOT include skip note when _skipped == 0."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(_verified=2, _skipped=0)
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert "skipped" not in out

    def test_differs_printed(self, capsys, mocker):
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(
            reports=[
                OverrideReport(
                    task_id="ingest",
                    service="glue",
                    resource_name="etl",
                    differs=[ParamConflict("WorkerType", "G.2X", "G.1X")],
                )
            ],
            _verified=1,
        )
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert "overrides" in out
        assert "differs:" in out
        assert "WorkerType" in out
        assert "G.1X" in out
        assert "G.2X" in out
        assert "declared:" in out
        assert "AWS default:" in out

    def test_silent_drop_printed(self, capsys, mocker):
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(
            reports=[
                OverrideReport(
                    task_id="ingest",
                    service="glue",
                    resource_name="etl",
                    silent_ignores=["--locked-arg"],
                )
            ],
            _verified=1,
        )
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert "non-overridable:" in out
        assert "--locked-arg" in out
        assert "silently ignore" in out
        assert "remove from glue_arguments" in out

    def test_matching_params_grouped_on_one_line(self, capsys, mocker):
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(
            reports=[
                OverrideReport(
                    task_id="t1",
                    service="glue",
                    resource_name="etl",
                    matching=["WorkerType", "MaxCapacity"],
                )
            ],
            _verified=1,
        )
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert "2 parameters match defaults" in out
        assert "WorkerType" in out
        assert "MaxCapacity" in out

    def test_single_matching_param_uses_singular_noun(self, capsys, mocker):
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(
            reports=[
                OverrideReport(
                    task_id="t1",
                    service="glue",
                    resource_name="etl",
                    matching=["WorkerType"],
                )
            ],
            _verified=1,
        )
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert "1 parameter matches defaults" in out

    def test_unverified_printed(self, capsys, mocker):
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(
            unverified=[ResourceRef(task_id="t1", kind="glue", ref="etl")],
        )
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert "override check skipped" in out
        assert "access denied, throttled, or other transient error" in out

    def test_unverified_singular_noun(self, capsys, mocker):
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(
            unverified=[ResourceRef(task_id="t1", kind="glue", ref="etl")],
        )
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert "1 resource" in out
        assert "resources" not in out

    def test_report_and_unverified_both_printed(self, capsys, mocker):
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(
            reports=[
                OverrideReport(
                    task_id="t1",
                    service="batch",
                    resource_name="jd",
                    differs=[ParamConflict("format", "json", "parquet")],
                )
            ],
            unverified=[ResourceRef(task_id="t2", kind="glue", ref="etl")],
            _verified=2,
        )
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert "overrides" in out
        assert "override check skipped" in out


# ---------------------------------------------------------------------------
# Integration: deploy_pipeline calls override check; --destroy skips it
# ---------------------------------------------------------------------------


def test_override_check_exception_does_not_abort_deploy(mocker, capsys):
    """An unexpected exception from override check is printed but does not abort deploy."""
    dag = _make_deploy_mocks(mocker, resource_result=ResourceCheckResult())
    mocker.patch(
        "polyris.deploy.check_resource_overrides",
        side_effect=RuntimeError("override boom"),
    )
    with pytest.raises(SystemExit) as exc:
        from polyris import deploy

        deploy.deploy_pipeline(dag)
    # deploy continued past the failed override check and hit the CFN template barrier
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "Override check failed unexpectedly" in out
    assert "RuntimeError" in out
    assert "override boom" in out


def test_deploy_pipeline_calls_override_check(mocker):
    dag = _make_deploy_mocks(mocker, resource_result=ResourceCheckResult())
    with pytest.raises(SystemExit) as exc:
        from polyris import deploy

        deploy.deploy_pipeline(dag)
    assert exc.value.code == 2
    deploy.check_resource_overrides.assert_called_once()
    # Verify failed_refs and region_warned wiring from existence check result
    call_kwargs = deploy.check_resource_overrides.call_args.kwargs
    assert "failed_refs" in call_kwargs
    assert call_kwargs["failed_refs"] is deploy.check_resource_references.return_value.failed_refs
    assert "region_warned" in call_kwargs
    assert call_kwargs["region_warned"] is deploy.check_resource_references.return_value.region_warned


def test_deploy_destroy_skips_override_check(mocker):
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
        "Stacks": [
            {
                "Outputs": [
                    {"OutputKey": "DependencyWrapperArn", "OutputValue": "arn:wrapper"},
                    {"OutputKey": "OrchestrationRoleArn", "OutputValue": "arn:role"},
                ],
                "Parameters": [],
            }
        ]
    }

    def _client(name, **kw):
        return {"sts": sts, "cloudformation": cfn}.get(name, mocker.MagicMock())

    session = mocker.MagicMock()
    session.client.side_effect = _client
    mocker.patch("polyris.deploy.boto3").Session.return_value = session

    ro_patch = mocker.patch("polyris.deploy.check_resource_overrides")
    rc_patch = mocker.patch("polyris.deploy.check_resource_references")

    from polyris import deploy

    deploy.deploy_pipeline(dag, destroy=True, dry_run=True)

    ro_patch.assert_not_called()
    rc_patch.assert_not_called()


# ---------------------------------------------------------------------------
# Completeness: _OVERRIDE_CHECKERS covers all TaskTypeLiterals
# ---------------------------------------------------------------------------


def test_override_checkers_covers_all_task_types():
    """Adding a TaskTypeLiteral without updating _OVERRIDE_CHECKERS fails here."""
    from polyris.constants import TaskTypeLiteral

    all_types = set(typing.get_args(TaskTypeLiteral))
    missing = all_types - set(_OVERRIDE_CHECKERS.keys())
    extra = set(_OVERRIDE_CHECKERS.keys()) - all_types
    assert not missing, f"TaskTypeLiterals not in _OVERRIDE_CHECKERS: {missing}"
    assert not extra, f"Keys in _OVERRIDE_CHECKERS not in TaskTypeLiteral: {extra}"


def test_override_info_entries_have_non_none_checkers():
    """Every task type in _OVERRIDE_INFO must have a non-None checker in _OVERRIDE_CHECKERS
    that is the same function object (identity check prevents silent divergence)."""
    for task_type in _OVERRIDE_INFO:
        checker = _OVERRIDE_CHECKERS.get(task_type)
        assert checker is not None, f"_OVERRIDE_CHECKERS[{task_type!r}] is None but {task_type!r} is in _OVERRIDE_INFO"
        assert checker is _OVERRIDE_INFO[task_type].checker, (
            f"_OVERRIDE_CHECKERS[{task_type!r}] is {checker!r} but "
            f"_OVERRIDE_INFO[{task_type!r}].checker is {_OVERRIDE_INFO[task_type].checker!r}"
        )


# ---------------------------------------------------------------------------
# T1: allocated_capacity match branch
# ---------------------------------------------------------------------------


class TestCheckGlueOverrideAllocatedCapacity:
    def test_allocated_capacity_matches(self, mocker):
        task = _make_task(mocker, task_type="glue", task_id="ingest", job_name="etl", allocated_capacity=10)
        report = _check_glue_override(task, {"AllocatedCapacity": 10})
        assert report is not None
        assert any("AllocatedCapacity" in m for m in report.matching)
        assert report.differs == []


# ---------------------------------------------------------------------------
# T2: Sanitization end-to-end through print_resource_override_result
# ---------------------------------------------------------------------------


class TestSanitizationEndToEnd:
    def test_control_chars_in_param_conflict_print_clean(self, capsys, mocker):
        """ParamConflict with \\n in name/values must produce clean printed output."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(
            reports=[
                OverrideReport(
                    task_id="t1",
                    service="glue",
                    resource_name="etl",
                    differs=[ParamConflict("WorkerType\ninjected", "G.2X\nX", "G.1X\nY")],
                )
            ],
            _verified=1,
        )
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert "WorkerType injected" in out
        assert "G.2X X" in out
        assert "G.1X Y" in out


# ---------------------------------------------------------------------------
# T3: failed_refs skip for ECS and Batch kinds in both checks
# ---------------------------------------------------------------------------


class TestFailedRefsBatchKinds:
    def _session(self, mocker, client_mock=None):
        session = mocker.MagicMock()
        if client_mock is not None:
            session.client.return_value = client_mock
        return session

    def test_batch_job_def_error_uses_batch_job_definition_kind(self, mocker):
        """Existence check: batch_job_definition failure → (batch_job_definition, ref) in failed_refs."""
        client = mocker.MagicMock()
        client.describe_job_definitions.return_value = {"jobDefinitions": []}
        task = _make_task(mocker, task_type="batch", job_queue="", job_definition="my-job-def")
        result = check_resource_references(_make_dag(mocker, [task]), self._session(mocker, client), "us-east-1")
        assert ("batch_job_definition", "my-job-def") in result.failed_refs

    def test_failed_refs_skip_ecs_task_def_in_override_check(self, mocker):
        """Override check: (ecs_task_definition, ref) in failed_refs → no API call, no report."""
        client = mocker.MagicMock()
        task = _make_task(
            mocker,
            task_type="ecs",
            task_definition="my-task-def",
            container_overrides={"containerOverrides": [{"name": "app", "cpu": 2048}]},
        )
        result = check_resource_overrides(
            _make_dag(mocker, [task]),
            self._session(mocker, client),
            "us-east-1",
            failed_refs={("ecs_task_definition", "my-task-def")},
        )
        assert result.reports == []
        client.describe_task_definition.assert_not_called()

    def test_failed_refs_skip_batch_job_def_in_override_check(self, mocker):
        """Override check: (batch_job_definition, ref) in failed_refs → no API call, no report."""
        client = mocker.MagicMock()
        task = _make_task(mocker, task_type="batch", job_definition="my-job-def", batch_parameters={"k": "v"})
        result = check_resource_overrides(
            _make_dag(mocker, [task]),
            self._session(mocker, client),
            "us-east-1",
            failed_refs={("batch_job_definition", "my-job-def")},
        )
        assert result.reports == []
        client.describe_job_definitions.assert_not_called()


# ---------------------------------------------------------------------------
# T5 / NEW-B1: containerOverrides entry without "name" key
# ---------------------------------------------------------------------------


class TestEcsOverrideWithoutContainerName:
    def test_container_override_without_name_key_produces_no_conflict(self, mocker):
        """Entry without 'name' key must be skipped — not matched against an empty container
        base (which would produce a false conflict for any declared command)."""
        task = _make_task(
            mocker,
            task_type="ecs",
            task_id="run_job",
            task_definition="my-task-def",
            container_overrides={
                "containerOverrides": [
                    {"command": ["python", "run.py"]},  # no "name" key
                ]
            },
        )
        task_def = {"containerDefinitions": [{"name": "app", "command": ["python", "old.py"]}]}
        assert _check_ecs_override(task, task_def) is None

    def test_container_override_with_empty_string_name_produces_no_conflict(self, mocker):
        """Entry with name='' (explicit empty string) must also be skipped, same as missing key."""
        task = _make_task(
            mocker,
            task_type="ecs",
            task_id="run_job",
            task_definition="my-task-def",
            container_overrides={
                "containerOverrides": [
                    {"name": "", "command": ["python", "run.py"]},
                ]
            },
        )
        task_def = {"containerDefinitions": [{"name": "app", "command": ["python", "old.py"]}]}
        assert _check_ecs_override(task, task_def) is None

    def test_named_and_unnamed_entries_mixed(self, mocker):
        """Unnamed entry skipped; named entry still compared normally."""
        task = _make_task(
            mocker,
            task_type="ecs",
            task_id="run_job",
            task_definition="my-task-def",
            container_overrides={
                "containerOverrides": [
                    {"command": ["bad"]},  # no "name" — skipped
                    {"name": "app", "cpu": 2048},  # named — compared
                ]
            },
        )
        task_def = {"containerDefinitions": [{"name": "app", "cpu": 1024}]}
        report = _check_ecs_override(task, task_def)
        assert report is not None
        assert len(report.differs) == 1
        assert report.differs[0].name == "cpu"


# ---------------------------------------------------------------------------
# T6: expired_token with already-fetched resource
# ---------------------------------------------------------------------------


class TestExpiredTokenWithCachedResource:
    def _session(self, mocker, client_mock=None):
        session = mocker.MagicMock()
        if client_mock is not None:
            session.client.return_value = client_mock
        return session

    def test_expired_token_does_not_discard_already_fetched_resource(self, mocker):
        """When expired_token fires on task B, task C referencing the same already-fetched
        resource as task A must still have its overrides checked — not routed to unverified."""
        client = mocker.MagicMock()
        client.get_job.side_effect = [
            {"Job": {"WorkerType": "G.1X"}},  # t1: etl-a → success, cached
            _client_error("ExpiredTokenException"),  # t2: etl-b → expires token
            # t3: etl-a is already in fetched — no API call needed
        ]
        tasks = [
            _make_task(
                mocker, task_type="glue", job_name="etl-a", worker_type="G.2X", task_id="t1"
            ),  # fetches, G.2X != G.1X → differs
            _make_task(
                mocker, task_type="glue", job_name="etl-b", worker_type="G.2X", task_id="t2"
            ),  # triggers expired_token
            _make_task(
                mocker, task_type="glue", job_name="etl-a", worker_type="G.1X", task_id="t3"
            ),  # same as t1, in fetched, G.1X == G.1X → matching
        ]
        result = check_resource_overrides(_make_dag(mocker, tasks), self._session(mocker, client), "us-east-1")
        assert client.get_job.call_count == 2  # etl-a + etl-b; t3 uses cache
        assert len(result.unverified) == 1  # only etl-b
        assert len(result.reports) == 2  # t1 (differs) and t3 (matching)


# ---------------------------------------------------------------------------
# B2: _fetch_batch_job_def_data raises _NotFoundError → orchestrator routes to unverified
# ---------------------------------------------------------------------------


class TestBatchFetcherNotFound:
    def _session(self, mocker, client_mock=None):
        session = mocker.MagicMock()
        if client_mock is not None:
            session.client.return_value = client_mock
        return session

    def test_batch_fetcher_not_found_routes_to_unverified(self, mocker):
        """_NotFoundError from fetcher (empty ACTIVE job defs) → resource unverified, no report."""
        client = mocker.MagicMock()
        client.describe_job_definitions.return_value = {"jobDefinitions": []}
        task = _make_task(mocker, task_type="batch", job_definition="jd", batch_parameters={"k": "v"})
        result = check_resource_overrides(_make_dag(mocker, [task]), self._session(mocker, client), "us-east-1")
        assert len(result.unverified) == 1
        assert result.reports == []
        assert result._verified == 0


# ---------------------------------------------------------------------------
# NEW-B2: ECS cluster PROVISIONING status treated as usable
# ---------------------------------------------------------------------------


class TestEcsClusterProvisioning:
    def test_provisioning_cluster_not_reported_as_missing(self, mocker):
        """A cluster in PROVISIONING state is being created — must not trigger a 'does not exist' error."""
        client = mocker.MagicMock()
        client.describe_clusters.return_value = {"clusters": [{"clusterName": "new-cluster", "status": "PROVISIONING"}]}
        from polyris.resource_check import _check_ecs_cluster

        _check_ecs_cluster(client, "new-cluster")  # must not raise


# ---------------------------------------------------------------------------
# ECS: container name present but not found in containerDefinitions → no conflict
# ---------------------------------------------------------------------------


class TestEcsOverrideUnknownContainerName:
    def test_command_override_for_absent_container_returns_none(self, mocker):
        """command override for a container name absent from containerDefinitions
        must return None — previously produced a spurious ParamConflict(actual=[])."""
        task = _make_task(
            mocker,
            task_type="ecs",
            task_id="run_job",
            task_definition="my-task-def",
            container_overrides={
                "containerOverrides": [
                    {"name": "ghost-container", "command": ["python", "run.py"]},
                ]
            },
        )
        task_def = {"containerDefinitions": [{"name": "app", "command": ["python", "old.py"]}]}
        assert _check_ecs_override(task, task_def) is None

    def test_cpu_override_for_absent_container_returns_none(self, mocker):
        """cpu override for absent container → no report."""
        task = _make_task(
            mocker,
            task_type="ecs",
            task_id="run_job",
            task_definition="my-task-def",
            container_overrides={
                "containerOverrides": [
                    {"name": "ghost", "cpu": 2048},
                ]
            },
        )
        task_def = {"containerDefinitions": [{"name": "app", "cpu": 1024}]}
        assert _check_ecs_override(task, task_def) is None

    def test_memory_override_for_absent_container_returns_none(self, mocker):
        """memory override for absent container → no report."""
        task = _make_task(
            mocker,
            task_type="ecs",
            task_id="run_job",
            task_definition="my-task-def",
            container_overrides={
                "containerOverrides": [
                    {"name": "ghost", "memory": 4096},
                ]
            },
        )
        task_def = {"containerDefinitions": [{"name": "app", "memory": 2048}]}
        assert _check_ecs_override(task, task_def) is None

    def test_absent_container_mixed_with_known_container(self, mocker):
        """Absent container entry is skipped; the known container is still compared."""
        task = _make_task(
            mocker,
            task_type="ecs",
            task_id="run_job",
            task_definition="my-task-def",
            container_overrides={
                "containerOverrides": [
                    {"name": "ghost", "cpu": 4096},  # not in task def → skipped
                    {"name": "app", "cpu": 2048},  # differs from 1024
                ]
            },
        )
        task_def = {"containerDefinitions": [{"name": "app", "cpu": 1024}]}
        report = _check_ecs_override(task, task_def)
        assert report is not None
        assert len(report.differs) == 1
        assert report.differs[0].name == "cpu"


# ---------------------------------------------------------------------------
# ECS: cpu/memory when task def has no container-level cpu (base_val = None)
# ---------------------------------------------------------------------------


class TestEcsOverrideCpuBaseValNone:
    def test_cpu_override_when_task_def_has_no_cpu_at_container_level_skipped(self, mocker):
        """ECS task def with no per-container cpu field: cpu override is silently
        skipped because _numeric_str(None) is False — guard is pinned."""
        task = _make_task(
            mocker,
            task_type="ecs",
            task_id="t1",
            task_definition="my-task-def",
            container_overrides={
                "containerOverrides": [
                    {"name": "app", "cpu": 2048},
                ]
            },
        )
        # containerDefinitions entry exists but has no "cpu" key
        task_def = {"containerDefinitions": [{"name": "app"}]}
        assert _check_ecs_override(task, task_def) is None

    def test_memory_override_when_task_def_has_no_memory_at_container_level_skipped(self, mocker):
        """ECS task def with no per-container memory field: memory override is silently
        skipped because _numeric_str(None) is False — symmetric to cpu guard."""
        task = _make_task(
            mocker,
            task_type="ecs",
            task_id="t1",
            task_definition="my-task-def",
            container_overrides={
                "containerOverrides": [
                    {"name": "app", "memory": 4096},
                ]
            },
        )
        task_def = {"containerDefinitions": [{"name": "app"}]}
        assert _check_ecs_override(task, task_def) is None


# ---------------------------------------------------------------------------
# Dedup: second task for already-failed resource does not retry API
# ---------------------------------------------------------------------------


class TestUnverifiedSeenNoRetry:
    def _session(self, mocker, client_mock):
        session = mocker.MagicMock()
        session.client.return_value = client_mock
        return session

    def test_second_task_for_failed_resource_does_not_retry_api(self, mocker):
        """When a resource fails to fetch, a second task referencing the same resource
        must not trigger a second API call — it's skipped via unverified_seen."""
        client = mocker.MagicMock()
        client.get_job.side_effect = [
            _client_error("AccessDeniedException"),
            # second task should NOT call get_job again
        ]
        tasks = [
            _make_task(mocker, task_type="glue", job_name="shared-job", worker_type="G.2X", task_id="t1"),
            _make_task(mocker, task_type="glue", job_name="shared-job", worker_type="G.1X", task_id="t2"),
        ]
        result = check_resource_overrides(_make_dag(mocker, tasks), self._session(mocker, client), "us-east-1")
        assert client.get_job.call_count == 1  # only one API call
        assert len(result.unverified) == 1  # only first ResourceRef added
        assert result.reports == []


# ---------------------------------------------------------------------------
# expired_token flag propagated to OverrideCheckResult
# ---------------------------------------------------------------------------


class TestExpiredTokenFlag:
    def _session(self, mocker, client_mock):
        session = mocker.MagicMock()
        session.client.return_value = client_mock
        return session

    def test_expired_token_exception_sets_flag(self, mocker):
        """ExpiredTokenException must set result.expired_token = True."""
        client = mocker.MagicMock()
        client.get_job.side_effect = _client_error("ExpiredTokenException")
        task = _make_task(mocker, task_type="glue", job_name="etl", worker_type="G.2X", task_id="t1")
        result = check_resource_overrides(_make_dag(mocker, [task]), self._session(mocker, client), "us-east-1")
        assert result.expired_token is True
        assert len(result.unverified) == 1
        assert result.access_denied_services == set()  # expired token ≠ IAM denial

    def test_non_expired_error_does_not_set_flag(self, mocker):
        """AccessDeniedException must not set expired_token."""
        client = mocker.MagicMock()
        client.get_job.side_effect = _client_error("AccessDeniedException")
        task = _make_task(mocker, task_type="glue", job_name="etl", worker_type="G.2X", task_id="t1")
        result = check_resource_overrides(_make_dag(mocker, [task]), self._session(mocker, client), "us-east-1")
        assert result.expired_token is False
        assert len(result.unverified) == 1

    def test_botocore_error_does_not_set_flag(self, mocker):
        """BotoCoreError (network/transport) must not set expired_token."""
        from botocore.exceptions import BotoCoreError

        client = mocker.MagicMock()
        client.get_job.side_effect = BotoCoreError()
        task = _make_task(mocker, task_type="glue", job_name="etl", worker_type="G.2X", task_id="t1")
        result = check_resource_overrides(_make_dag(mocker, [task]), self._session(mocker, client), "us-east-1")
        assert result.expired_token is False
        assert len(result.unverified) == 1


# ---------------------------------------------------------------------------
# print_resource_override_result: expired_token message vs generic message
# ---------------------------------------------------------------------------


class TestPrintOverrideResultExpiredTokenMessage:
    def test_expired_token_produces_credentials_message(self, capsys, mocker):
        """expired_token=True must produce a 'credentials expired' message."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(
            unverified=[ResourceRef(task_id="t1", kind="glue", ref="etl")],
            expired_token=True,
            _verified=0,
        )
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert "expired" in out.lower()
        assert "re-authenticate" in out.lower()
        assert "re-run" in out.lower()
        assert "access denied" not in out.lower()

    def test_non_expired_unverified_produces_access_denied_throttled_message(self, capsys, mocker):
        """expired_token=False must produce the generic unverified message (not the expired-token message)."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(
            unverified=[ResourceRef(task_id="t1", kind="glue", ref="etl")],
            expired_token=False,
            _verified=0,
        )
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert "access denied" in out.lower()
        assert "throttled" in out.lower()
        assert "transient" in out.lower()
        assert "expired" not in out.lower()


# ---------------------------------------------------------------------------
# print_resource_override_result: blank line separator before output
# ---------------------------------------------------------------------------


class TestPrintOverrideResultBlankLine:
    def test_blank_line_before_success_output(self, capsys, mocker):
        """print_resource_override_result must emit a blank line before the success line."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(_verified=1)
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert out.startswith("\n")

    def test_blank_line_before_warning_output(self, capsys, mocker):
        """print_resource_override_result must emit a blank line before warning output."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(
            reports=[
                OverrideReport(
                    task_id="t1",
                    service="glue",
                    resource_name="etl",
                    differs=[ParamConflict("WorkerType", "G.2X", "G.1X")],
                )
            ],
            _verified=1,
        )
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert out.startswith("\n")

    def test_no_output_when_nothing_to_report(self, capsys, mocker):
        """With no reports, no unverified, and checked==0, nothing is printed."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(_verified=0)
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert out == ""

    def test_blank_line_between_reports_and_unverified(self, capsys, mocker):
        """A blank line must separate the reports block from the unverified block."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(
            reports=[
                OverrideReport(
                    task_id="t1",
                    service="glue",
                    resource_name="job-a",
                    differs=[ParamConflict("WorkerType", "G.2X", "G.1X")],
                )
            ],
            unverified=[ResourceRef(task_id="t2", kind="batch", ref="jd")],
            _verified=1,
        )
        print_resource_override_result(result)
        out = capsys.readouterr().out
        pos_reports = out.index("job-a")
        pos_unverified = out.index("override check skipped")
        between = out[pos_reports:pos_unverified]
        assert "\n\n" in between


# ---------------------------------------------------------------------------
# print_resource_override_result: multiple reports all printed
# ---------------------------------------------------------------------------


class TestPrintOverrideResultMultipleReports:
    def test_multiple_reports_all_appear_in_output(self, capsys, mocker):
        """Two reports must both appear in the output — the loop is not short-circuited."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(
            reports=[
                OverrideReport(
                    task_id="t1",
                    service="glue",
                    resource_name="etl-job",
                    differs=[ParamConflict("WorkerType", "G.2X", "G.1X")],
                ),
                OverrideReport(task_id="t2", service="batch", resource_name="my-job-def", matching=["param-a"]),
            ],
            _verified=2,
        )
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert "etl-job" in out
        assert "my-job-def" in out
        assert "WorkerType" in out
        assert "param-a" in out

    def test_blank_line_between_reports(self, capsys, mocker):
        """A blank line must separate consecutive reports in the output."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(
            reports=[
                OverrideReport(
                    task_id="t1",
                    service="glue",
                    resource_name="job-a",
                    differs=[ParamConflict("WorkerType", "G.2X", "G.1X")],
                ),
                OverrideReport(
                    task_id="t2",
                    service="glue",
                    resource_name="job-b",
                    differs=[ParamConflict("NumberOfWorkers", "5", "2")],
                ),
            ],
            _verified=2,
        )
        print_resource_override_result(result)
        out = capsys.readouterr().out
        # job-a block must appear before job-b block with an empty line between them
        pos_a = out.index("job-a")
        pos_b = out.index("job-b")
        between = out[pos_a:pos_b]
        assert "\n\n" in between


# ---------------------------------------------------------------------------
# deploy_pipeline --dry-run without --destroy: both checks still run
# ---------------------------------------------------------------------------


def test_deploy_dry_run_without_destroy_runs_both_checks(mocker):
    """--dry-run without --destroy must still invoke both resource checks."""
    dag = _make_deploy_mocks(mocker, resource_result=ResourceCheckResult())
    with pytest.raises(SystemExit) as exc:
        from polyris import deploy

        deploy.deploy_pipeline(dag, dry_run=True)
    assert exc.value.code == 2  # hit _generate_cfn_template barrier — not a check failure
    deploy.check_resource_references.assert_called_once()
    deploy.check_resource_overrides.assert_called_once()


# ---------------------------------------------------------------------------
# Fix #1: _skipped note visible when reports are present
# ---------------------------------------------------------------------------


class TestSkipNoteWithReports:
    def test_skip_note_shown_alongside_reports(self, capsys, mocker):
        """When there are override reports AND skipped resources, the skip count
        must appear in the output — user should not wonder why some tasks are absent."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(
            reports=[
                OverrideReport(
                    task_id="t1",
                    service="glue",
                    resource_name="etl",
                    differs=[ParamConflict("WorkerType", "G.2X", "G.1X")],
                )
            ],
            _verified=1,
            _skipped=2,
        )
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert "2" in out
        assert "skipped" in out
        assert "existence check" in out

    def test_no_skip_note_when_skipped_is_zero(self, capsys, mocker):
        """When _skipped == 0, no skip note must appear."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(
            reports=[
                OverrideReport(
                    task_id="t1",
                    service="glue",
                    resource_name="etl",
                    differs=[ParamConflict("WorkerType", "G.2X", "G.1X")],
                )
            ],
            _verified=1,
            _skipped=0,
        )
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert "skipped" not in out


# ---------------------------------------------------------------------------
# Fix #2: message when _verified=0 and _skipped>0 (all tasks failed existence check)
# ---------------------------------------------------------------------------


class TestAllSkippedOutput:
    def test_all_skipped_prints_warning_message(self, capsys, mocker):
        """When _verified=0 and _skipped>0 (all override tasks had existence failures),
        a warning must appear — user should know the check ran but was fully skipped."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(_verified=0, _skipped=3)
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert out != ""
        assert "3" in out
        assert "skipped" in out
        assert "existence check" in out

    def test_all_skipped_blank_line_before_message(self, capsys, mocker):
        """The all-skipped message must start with a blank line like all other output blocks."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(_verified=0, _skipped=1)
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert out.startswith("\n")

    def test_zero_verified_zero_skipped_no_output(self, capsys, mocker):
        """When both _verified and _skipped are 0, nothing is printed (no Glue/ECS/Batch tasks)."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(_verified=0, _skipped=0)
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert out == ""


# ---------------------------------------------------------------------------
# _skipped deduplication: two tasks same failed resource → _skipped == 1
# ---------------------------------------------------------------------------


class TestSkippedCounterDedup:
    def _session(self, mocker, client_mock=None):
        session = mocker.MagicMock()
        if client_mock is not None:
            session.client.return_value = client_mock
        return session

    def test_failed_ref_skipped_counter_deduped_for_same_resource(self, mocker):
        """Two tasks referencing the same failed resource → _skipped == 1, not 2."""
        client = mocker.MagicMock()
        tasks = [
            _make_task(mocker, task_type="glue", job_name="etl", worker_type="G.2X", task_id="t1"),
            _make_task(mocker, task_type="glue", job_name="etl", worker_type="G.1X", task_id="t2"),
        ]
        result = check_resource_overrides(
            _make_dag(mocker, tasks),
            self._session(mocker, client),
            "us-east-1",
            failed_refs={("glue", "etl")},
        )
        assert result._skipped == 1  # deduped, not 2
        assert result.reports == []
        client.get_job.assert_not_called()


# ---------------------------------------------------------------------------
# ECS cpu string=int equal: "1024" vs 1024 → matching, not conflict
# ---------------------------------------------------------------------------


class TestEcsCpuStringEqualInt:
    def test_cpu_digit_string_equal_to_int_produces_matching(self, mocker):
        """cpu declared as digit-string '1024' matching actual int 1024 → matching entry."""
        task = _make_task(
            mocker,
            task_type="ecs",
            task_id="t1",
            task_definition="my-task-def",
            container_overrides={"containerOverrides": [{"name": "app", "cpu": "1024"}]},
        )
        task_def = {"containerDefinitions": [{"name": "app", "cpu": 1024}]}
        report = _check_ecs_override(task, task_def)
        assert report is not None
        assert any("cpu=1024" in m for m in report.matching)
        assert not any(c.name == "cpu" for c in report.differs)

    def test_cpu_float_string_equal_to_int_produces_matching(self, mocker):
        """cpu declared as '1024.0' (float-as-string) normalizes to int 1024; matches actual 1024."""
        task = _make_task(
            mocker,
            task_type="ecs",
            task_id="t1",
            task_definition="my-task-def",
            container_overrides={"containerOverrides": [{"name": "app", "cpu": "1024.0"}]},
        )
        task_def = {"containerDefinitions": [{"name": "app", "cpu": 1024}]}
        report = _check_ecs_override(task, task_def)
        assert report is not None
        assert any("cpu=1024" in m for m in report.matching)
        assert not any(c.name == "cpu" for c in report.differs)


# ---------------------------------------------------------------------------
# _numeric_str: float-as-string accepted
# ---------------------------------------------------------------------------


class TestNumericStrFloatString:
    def test_float_string_is_numeric(self):
        """'512.0' (float-formatted integer from AWS) must be accepted as numeric."""
        assert _numeric_str("512.0") is True

    def test_empty_string_is_not_numeric(self):
        assert _numeric_str("") is False


# ---------------------------------------------------------------------------
# Glue: DefaultArguments key entirely absent (not just empty dict)
# ---------------------------------------------------------------------------


class TestGlueArgumentDefaultsKeyAbsent:
    def test_glue_argument_no_default_args_key_at_all_not_reported(self, mocker):
        """When the job dict has no DefaultArguments key (not even empty), new args are silent."""
        task = _make_task(
            mocker, task_type="glue", task_id="ingest", job_name="etl", glue_arguments={"--new-arg": "value"}
        )
        job = {}  # no DefaultArguments key at all — different from {"DefaultArguments": {}}
        assert _check_glue_override(task, job) is None


# ---------------------------------------------------------------------------
# Batch: revision key present but value None — must not crash
# ---------------------------------------------------------------------------


class TestFetchBatchJobDefRevisionNone:
    def test_revision_key_present_value_none_does_not_crash(self, mocker):
        """revision key exists but value is None → 'or 0' treats it as 0; must not raise TypeError."""
        client = mocker.MagicMock()
        client.describe_job_definitions.return_value = {
            "jobDefinitions": [{"jobDefinitionName": "jd", "revision": None, "parameters": {"k": "v"}}]
        }
        result = _fetch_batch_job_def_data(client, "jd")
        assert result["parameters"]["k"] == "v"

    def test_revision_none_vs_integer_max_selects_integer(self, mocker):
        """When one entry has revision=None and another has revision=5, revision 5 wins."""
        client = mocker.MagicMock()
        client.describe_job_definitions.return_value = {
            "jobDefinitions": [
                {"jobDefinitionName": "jd", "revision": None, "parameters": {"k": "old"}},
                {"jobDefinitionName": "jd", "revision": 5, "parameters": {"k": "new"}},
            ]
        }
        result = _fetch_batch_job_def_data(client, "jd")
        assert result["parameters"]["k"] == "new"


# ---------------------------------------------------------------------------
# print_resource_override_result: skip note when reports + unverified + _skipped
# ---------------------------------------------------------------------------


class TestSkipNoteWithReportsAndUnverified:
    def test_skip_note_at_end_when_reports_and_unverified_and_skipped(self, capsys, mocker):
        """When reports, unverified, AND _skipped > 0 are all present,
        the skip note appears last — after the unverified message."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(
            reports=[
                OverrideReport(
                    task_id="t1",
                    service="glue",
                    resource_name="job-a",
                    differs=[ParamConflict("WorkerType", "G.2X", "G.1X")],
                )
            ],
            unverified=[ResourceRef(task_id="t2", kind="batch", ref="jd")],
            _verified=1,
            _skipped=2,
        )
        print_resource_override_result(result)
        out = capsys.readouterr().out
        pos_unverified = out.index("override check skipped")
        pos_skip_note = out.index("2 resources skipped")
        assert pos_skip_note > pos_unverified  # skip note comes after unverified


# ---------------------------------------------------------------------------
# Region fallback warning
# ---------------------------------------------------------------------------


class TestRegionFallbackWarning:
    def _session(self, mocker, region=None):
        session = mocker.MagicMock()
        session.region_name = region
        return session

    def test_existence_check_warns_when_no_region(self, mocker, capsys):
        """When region=None and session.region_name=None, existence check prints a warning."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        task = _make_task(mocker, task_type="glue", task_id="t1", job_name="etl")
        client = mocker.MagicMock()
        client.get_job.return_value = {}
        session = self._session(mocker, region=None)
        session.client.return_value = client

        check_resource_references(_make_dag(mocker, [task]), session, region=None)
        out = capsys.readouterr().out
        assert "no AWS region configured" in out
        assert "us-east-1" in out

    def test_existence_check_no_warn_when_region_provided(self, mocker, capsys):
        """When region is explicitly set, no region warning must appear."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        task = _make_task(mocker, task_type="glue", task_id="t1", job_name="etl")
        client = mocker.MagicMock()
        client.get_job.return_value = {}
        session = self._session(mocker, region=None)
        session.client.return_value = client

        check_resource_references(_make_dag(mocker, [task]), session, region="eu-west-1")
        out = capsys.readouterr().out
        assert "no AWS region configured" not in out

    def test_existence_check_no_warn_for_empty_dag(self, mocker, capsys):
        """Empty DAG → no refs → no region warning (avoid noise for no-op check)."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        session = self._session(mocker, region=None)
        check_resource_references(_make_dag(mocker, []), session, region=None)
        out = capsys.readouterr().out
        assert "no region configured" not in out

    def test_override_check_warns_when_no_region(self, mocker, capsys):
        """Override check also warns when region falls back to us-east-1."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        task = _make_task(mocker, task_type="glue", task_id="t1", job_name="etl", worker_type="G.2X")
        client = mocker.MagicMock()
        client.get_job.return_value = {"Job": {"WorkerType": "G.1X"}}
        session = self._session(mocker, region=None)
        session.client.return_value = client

        check_resource_overrides(_make_dag(mocker, [task]), session, region=None)
        out = capsys.readouterr().out
        assert "no AWS region configured" in out


# ---------------------------------------------------------------------------
# Override check: IAM hint when access denied
# ---------------------------------------------------------------------------


class TestOverrideIamHint:
    def _session(self, mocker, client):
        session = mocker.MagicMock()
        session.region_name = "us-east-1"
        session.client.return_value = client
        return session

    def test_iam_hint_printed_for_glue_access_denied(self, mocker, capsys):
        """When glue:GetJob returns AccessDeniedException, IAM hint appears in output."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        client = mocker.MagicMock()
        client.get_job.side_effect = _client_error("AccessDeniedException")
        task = _make_task(mocker, task_type="glue", task_id="t1", job_name="etl", worker_type="G.2X")
        result = check_resource_overrides(
            _make_dag(mocker, [task]),
            self._session(mocker, client),
            "us-east-1",
        )
        assert "glue" in result.access_denied_services

        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert "glue:GetJob" in out
        assert "deploy role" in out

    def test_iam_hint_printed_for_ecs_access_denied(self, mocker, capsys):
        """When ecs:DescribeTaskDefinition returns AccessDenied, IAM hint names the action."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        client = mocker.MagicMock()
        client.describe_task_definition.side_effect = _client_error("AccessDenied")
        task = _make_task(
            mocker,
            task_type="ecs",
            task_id="t1",
            task_definition="my-def",
            container_overrides={"containerOverrides": [{"name": "app", "cpu": 2048}]},
        )
        result = check_resource_overrides(
            _make_dag(mocker, [task]),
            self._session(mocker, client),
            "us-east-1",
        )
        assert "ecs" in result.access_denied_services

        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert "ecs:DescribeTaskDefinition" in out

    def test_no_iam_hint_for_throttle(self, mocker, capsys):
        """ThrottlingException must NOT produce an IAM hint — throttle ≠ permission issue."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        client = mocker.MagicMock()
        client.get_job.side_effect = _client_error("ThrottlingException")
        task = _make_task(mocker, task_type="glue", task_id="t1", job_name="etl", worker_type="G.2X")
        result = check_resource_overrides(
            _make_dag(mocker, [task]),
            self._session(mocker, client),
            "us-east-1",
        )
        assert not result.access_denied_services

        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert "deploy role" not in out


# ---------------------------------------------------------------------------
# ECS task-level cpu/memory
# ---------------------------------------------------------------------------


class TestEcsTaskLevelResources:
    def test_task_level_cpu_differs(self, mocker):
        """Task-level cpu in container_overrides differs from task definition → in differs."""
        task = _make_task(
            mocker, task_type="ecs", task_id="t1", task_definition="my-def", container_overrides={"cpu": 2048}
        )
        task_def = {"cpu": "1024", "containerDefinitions": []}
        report = _check_ecs_override(task, task_def)
        assert report is not None
        assert any(c.name == "task.cpu" and c.declared == 2048 and c.actual == 1024 for c in report.differs)

    def test_task_level_cpu_matches(self, mocker):
        """Task-level cpu matches task definition → in matching, not differs."""
        task = _make_task(
            mocker, task_type="ecs", task_id="t1", task_definition="my-def", container_overrides={"cpu": 1024}
        )
        task_def = {"cpu": "1024", "containerDefinitions": []}
        report = _check_ecs_override(task, task_def)
        assert report is not None
        assert report.differs == []
        assert any("task.cpu=1024" in m for m in report.matching)

    def test_task_level_memory_differs(self, mocker):
        """Task-level memory in container_overrides differs from task definition → in differs."""
        task = _make_task(
            mocker, task_type="ecs", task_id="t1", task_definition="my-def", container_overrides={"memory": 4096}
        )
        task_def = {"memory": "2048", "containerDefinitions": []}
        report = _check_ecs_override(task, task_def)
        assert report is not None
        assert any(c.name == "task.memory" and c.declared == 4096 and c.actual == 2048 for c in report.differs)

    def test_task_level_no_baseline_produces_scope_note(self, mocker):
        """Task-level cpu declared but task def has no top-level cpu (EC2 launch type)
        → scope_note in report, not a diff."""
        task = _make_task(
            mocker, task_type="ecs", task_id="t1", task_definition="my-def", container_overrides={"cpu": 2048}
        )
        task_def = {"containerDefinitions": []}  # no top-level cpu (EC2 launch type)
        report = _check_ecs_override(task, task_def)
        assert report is not None
        assert report.differs == []
        assert report.matching == []
        assert any("task.cpu" in n and "EC2" in n for n in report.scope_notes)

    def test_task_level_cpu_as_string(self, mocker):
        """Task-level cpu declared as string — normalized to int before comparison."""
        task = _make_task(
            mocker, task_type="ecs", task_id="t1", task_definition="my-def", container_overrides={"cpu": "2048"}
        )
        task_def = {"cpu": "1024", "containerDefinitions": []}
        report = _check_ecs_override(task, task_def)
        assert report is not None
        assert any(c.name == "task.cpu" and c.declared == 2048 and c.actual == 1024 for c in report.differs)

    def test_task_level_non_numeric_silently_skipped(self, mocker):
        """Task-level cpu declared as non-numeric string — silently skipped, no entry in report."""
        task = _make_task(
            mocker, task_type="ecs", task_id="t1", task_definition="my-def", container_overrides={"cpu": "auto"}
        )
        task_def = {"cpu": "1024", "containerDefinitions": []}
        report = _check_ecs_override(task, task_def)
        assert report is None  # guard rejects "auto", nothing to report

    def test_task_level_both_cpu_and_memory_one_differs_one_matches(self, mocker):
        """Both cpu and memory at task level — cpu differs, memory matches — independent entries."""
        task = _make_task(
            mocker,
            task_type="ecs",
            task_id="t1",
            task_definition="my-def",
            container_overrides={"cpu": 2048, "memory": 4096},
        )
        task_def = {"cpu": "1024", "memory": "4096", "containerDefinitions": []}
        report = _check_ecs_override(task, task_def)
        assert report is not None
        assert any(c.name == "task.cpu" and c.declared == 2048 for c in report.differs)
        assert any("task.memory=4096" in m for m in report.matching)

    def test_task_level_and_container_level_both_handled(self, mocker):
        """Task-level and container-level overrides both compared independently."""
        task = _make_task(
            mocker,
            task_type="ecs",
            task_id="t1",
            task_definition="my-def",
            container_overrides={
                "cpu": 2048,
                "containerOverrides": [{"name": "app", "cpu": 1024}],
            },
        )
        task_def = {"cpu": "1024", "containerDefinitions": [{"name": "app", "cpu": 512}]}
        report = _check_ecs_override(task, task_def)
        assert report is not None
        assert any(c.name == "task.cpu" for c in report.differs)  # task-level differs
        assert any(c.name == "cpu" for c in report.differs)  # container-level differs

    def test_no_task_level_keys_no_task_level_entries(self, mocker):
        """container_overrides without task-level cpu/memory produces no task.* entries."""
        task = _make_task(
            mocker,
            task_type="ecs",
            task_id="t1",
            task_definition="my-def",
            container_overrides={"containerOverrides": [{"name": "app", "cpu": 2048}]},
        )
        task_def = {"cpu": "1024", "containerDefinitions": [{"name": "app", "cpu": 2048}]}
        report = _check_ecs_override(task, task_def)
        assert report is not None
        assert not any(c.name.startswith("task.") for c in report.differs)
        assert not any(m.startswith("task.") for m in report.matching)

    def test_scope_note_printed_in_output(self, capsys, mocker):
        """scope_notes (from other services or future use) appear in print output."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(
            reports=[
                OverrideReport(
                    task_id="t1",
                    service="ecs",
                    resource_name="my-def",
                    scope_notes=["task.cpu — EC2 task definition has no task-level cpu limit (not compared)"],
                )
            ],
            _verified=1,
        )
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert "not checked:" in out
        assert "EC2 task definition" in out

    def test_scope_note_only_report_does_not_trigger_override_warning_header(self, capsys, mocker):
        """scope_notes-only OverrideReport must not cause '⚠  resource parameter overrides:' header."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(
            reports=[
                OverrideReport(
                    task_id="t1",
                    service="ecs",
                    resource_name="my-def",
                    scope_notes=["task.cpu — EC2 task definition has no task-level cpu limit (not compared)"],
                )
            ],
            _verified=1,
        )
        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert "resource parameter overrides" not in out
        assert "✓" in out
        assert "not checked:" in out


# ---------------------------------------------------------------------------
# _OVERRIDE_IAM_ACTION completeness parity
# ---------------------------------------------------------------------------


class TestOverrideIamActionParity:
    def test_override_iam_action_covers_all_override_info_services(self):
        """_OVERRIDE_IAM_ACTION must have an entry for every service in _OVERRIDE_INFO."""
        expected = {info.service for info in _OVERRIDE_INFO.values()}
        assert set(_OVERRIDE_IAM_ACTION.keys()) == expected


# ---------------------------------------------------------------------------
# Region warning fires only when API calls will actually be made
# ---------------------------------------------------------------------------


class TestRegionWarningPrecision:
    def _session(self, mocker, region=None):
        session = mocker.MagicMock()
        session.region_name = region
        return session

    def test_existence_check_no_warn_for_cross_role_only_dag(self, mocker, capsys):
        """Cross-role tasks are skipped without API calls — no region warning must fire."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        task = _make_task(
            mocker, task_type="glue", task_id="t1", job_name="etl", role="arn:aws:iam::123456789012:role/other-role"
        )
        session = self._session(mocker, region=None)

        check_resource_references(_make_dag(mocker, [task]), session, region=None)
        out = capsys.readouterr().out
        assert "no region configured" not in out

    def test_override_check_no_warn_when_all_tasks_in_failed_refs(self, mocker, capsys):
        """When all override-eligible tasks are in failed_refs, no region warning must fire."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        task = _make_task(mocker, task_type="glue", task_id="t1", job_name="etl", worker_type="G.2X")
        session = self._session(mocker, region=None)
        session.client.return_value = mocker.MagicMock()

        check_resource_overrides(
            _make_dag(mocker, [task]),
            session,
            region=None,
            failed_refs={("glue", "etl")},
        )
        out = capsys.readouterr().out
        assert "no region configured" not in out


# ---------------------------------------------------------------------------
# batch access-denied → IAM hint path
# ---------------------------------------------------------------------------


class TestOverrideIamHintBatch:
    def _session(self, mocker, client):
        session = mocker.MagicMock()
        session.region_name = "us-east-1"
        session.client.return_value = client
        return session

    def test_iam_hint_printed_for_batch_access_denied(self, mocker, capsys):
        """When batch:DescribeJobDefinitions returns AccessDeniedException, IAM hint appears."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        client = mocker.MagicMock()
        client.describe_job_definitions.side_effect = _client_error("AccessDeniedException")
        task = _make_task(mocker, task_type="batch", task_id="t1", job_definition="jd", batch_parameters={"key": "val"})
        result = check_resource_overrides(
            _make_dag(mocker, [task]),
            self._session(mocker, client),
            "us-east-1",
        )
        assert "batch" in result.access_denied_services

        print_resource_override_result(result)
        out = capsys.readouterr().out
        assert "batch:DescribeJobDefinitions" in out
        assert "deploy role" in out


# ---------------------------------------------------------------------------
# Two-phase throttle: existence throttle ⊄ failed_refs → override re-attempts → unverified
# ---------------------------------------------------------------------------


def test_two_phase_throttle_contract(mocker):
    """Throttle in existence check must NOT populate failed_refs; override check therefore
    re-attempts the same resource, and if that also throttles the resource goes to unverified."""
    session = mocker.MagicMock()
    client = mocker.MagicMock()
    session.client.return_value = client

    # Existence check: glue job throttles
    client.get_job.side_effect = _client_error("ThrottlingException")
    task = _make_task(mocker, task_type="glue", job_name="etl", worker_type="G.2X", task_id="t1")
    dag = _make_dag(mocker, [task])

    existence_result = check_resource_references(dag, session, "us-east-1")
    assert len(existence_result.warnings) == 1
    assert ("glue", "etl") not in existence_result.failed_refs  # throttle excluded

    # Override check: same resource, still throttles
    client.get_job.side_effect = _client_error("ThrottlingException")
    override_result = check_resource_overrides(dag, session, "us-east-1", failed_refs=existence_result.failed_refs)
    assert len(override_result.unverified) == 1
    assert override_result.unverified[0].ref == "etl"
    # Both phases called get_job: existence check once, override check once
    assert client.get_job.call_count == 2


# ---------------------------------------------------------------------------
# No duplicate region warning when both checks run without a configured region
# ---------------------------------------------------------------------------


def test_no_duplicate_region_warning_when_both_checks_run(mocker, capsys):
    """When both checks run with no region configured, the 'no AWS region' warning
    must appear exactly once — region_warned is threaded from existence to override."""
    session = mocker.MagicMock()
    session.region_name = None
    client = mocker.MagicMock()
    session.client.return_value = client
    mocker.patch("sys.stdout.isatty", return_value=False)

    task = _make_task(mocker, task_type="glue", job_name="etl", worker_type="G.2X", task_id="t1")
    dag = _make_dag(mocker, [task])

    client.get_job.return_value = {
        "Job": {
            "Name": "etl",
            "Command": {"ScriptLocation": "s3://bucket/script.py"},
            "DefaultArguments": {},
            "NonOverridableArguments": {},
            "WorkerType": "G.1X",
            "MaxCapacity": 2.0,
        }
    }

    rc_result = check_resource_references(dag, session, None)
    check_resource_overrides(
        dag, session, None, failed_refs=rc_result.failed_refs, region_warned=rc_result.region_warned
    )

    out = capsys.readouterr().out
    assert out.count("no AWS region configured") == 1


# ---------------------------------------------------------------------------
# print_resource_override_result: findings printed before scope_reports
# ---------------------------------------------------------------------------


class TestFindingsBeforeScopeReports:
    def test_findings_printed_before_scope_reports(self, capsys, mocker):
        """When a report has both differs and scope_notes, differs must appear before
        scope_notes — a user reading output top-down sees actionable conflicts first."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(
            reports=[
                OverrideReport(
                    task_id="t1",
                    service="ecs",
                    resource_name="my-def",
                    differs=[ParamConflict(name="cpu", declared="2048", actual="1024")],
                    scope_notes=["task.memory — EC2 task definition has no task-level memory limit (not compared)"],
                )
            ],
            _verified=1,
        )
        print_resource_override_result(result)
        out = capsys.readouterr().out
        pos_differs = out.index("cpu")
        pos_scope = out.index("not checked:")
        assert pos_differs < pos_scope, "differs must appear before scope_notes in output"

    def test_blank_line_between_findings_block_and_scope_block(self, capsys, mocker):
        """A blank line must separate the findings block from the scope_only block
        when there are two separate reports: one with conflicts and one with only scope_notes."""
        mocker.patch("sys.stdout.isatty", return_value=False)
        result = OverrideCheckResult(
            reports=[
                OverrideReport(
                    task_id="t1",
                    service="ecs",
                    resource_name="my-def",
                    differs=[ParamConflict(name="cpu", declared="2048", actual="1024")],
                ),
                OverrideReport(
                    task_id="t2",
                    service="ecs",
                    resource_name="other-def",
                    scope_notes=["task.memory — EC2 task definition has no task-level memory limit (not compared)"],
                ),
            ],
            _verified=2,
        )
        print_resource_override_result(result)
        out = capsys.readouterr().out
        # The blank line sits between the findings block and the scope_reports block
        pos_differs = out.index("cpu")
        pos_scope = out.index("not checked:")
        between = out[pos_differs:pos_scope]
        assert "\n\n" in between, "blank line expected between findings block and scope_reports block"
