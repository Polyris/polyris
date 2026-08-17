"""Tests for the new deploy-time UX pieces:

* ``stack_name`` accessor on ``PolyrisConfig`` and ``_StageConfig``
* ``_warn_conflicts`` — loud override warnings when CLI ≠ config
* ``_print_deploy_summary`` — the pre-deploy target block
* ``_fail_no_stack_name`` — actionable error when nothing resolves a stack name
* ``_read_sam_stack`` — CFN describe_stacks reader, with "stack not found" path

Mocking follows Principle #14: boto3 is patched at the SDK boundary (via
``session.client``); everything above it is real code exercising real logic.
"""
from __future__ import annotations

import pytest

from polyris.config import PolyrisConfig, _StageConfig
from polyris import deploy as deploy_mod


# ─────────────────────────────────────────────────────────────────────
#  stack_name accessor (config.py)
# ─────────────────────────────────────────────────────────────────────


def _cfg(environments, default_stage="dev"):
    """Build a PolyrisConfig without touching the filesystem."""
    c = PolyrisConfig.__new__(PolyrisConfig)  # bypass __init__/_load
    c._environments = environments
    c._default_stage = default_stage
    return c


def _clear_stack_env(monkeypatch):
    for var in ("POLYRIS_STACK", "POLYRIS_STACK_NAME"):
        monkeypatch.delenv(var, raising=False)


class TestStackNameOnPolyrisConfig:
    def test_reads_from_env_config(self, monkeypatch):
        _clear_stack_env(monkeypatch)
        c = _cfg({"dev": {"stack_name": "acme-dev"}})
        assert c.stack_name == "acme-dev"

    def test_returns_none_when_not_set(self, monkeypatch):
        _clear_stack_env(monkeypatch)
        c = _cfg({"dev": {"namespace": "acme"}})
        assert c.stack_name is None

    def test_env_var_polyris_stack_wins(self, monkeypatch):
        monkeypatch.setenv("POLYRIS_STACK", "from-env")
        c = _cfg({"dev": {"stack_name": "from-config"}})
        assert c.stack_name == "from-env"

    def test_env_var_polyris_stack_name_also_wins(self, monkeypatch):
        _clear_stack_env(monkeypatch)
        monkeypatch.setenv("POLYRIS_STACK_NAME", "from-legacy-env")
        c = _cfg({"dev": {"stack_name": "from-config"}})
        assert c.stack_name == "from-legacy-env"


class TestStackNameOnStageConfig:
    def _stage(self, environments, stage="dev"):
        parent = _cfg(environments, default_stage=stage)
        return _StageConfig(parent, stage)

    def test_reads_from_env_config(self, monkeypatch):
        _clear_stack_env(monkeypatch)
        sc = self._stage({"dev": {"stack_name": "acme-dev"}})
        assert sc.stack_name == "acme-dev"

    def test_returns_none_when_not_set(self, monkeypatch):
        _clear_stack_env(monkeypatch)
        sc = self._stage({"dev": {}})
        assert sc.stack_name is None

    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("POLYRIS_STACK", "override")
        sc = self._stage({"dev": {"stack_name": "config"}})
        assert sc.stack_name == "override"

    def test_stage_view_reads_the_requested_stage_not_default(self, monkeypatch):
        # A bug worth pinning: `PolyrisConfig.namespace` used to read from
        # DEFAULT_STAGE even when --stage prod was passed. _StageConfig fixes
        # that — verify stack_name follows the same rule.
        _clear_stack_env(monkeypatch)
        parent = _cfg(
            {
                "dev": {"stack_name": "acme-dev"},
                "prod": {"stack_name": "acme-prod"},
            },
            default_stage="dev",
        )
        sc_prod = _StageConfig(parent, "prod")
        assert sc_prod.stack_name == "acme-prod"


# ─────────────────────────────────────────────────────────────────────
#  _warn_conflicts
# ─────────────────────────────────────────────────────────────────────


class TestWarnConflicts:
    def test_prints_warning_when_cli_overrides_config(self, capsys):
        deploy_mod._warn_conflicts(
            cli={"region": "us-west-2"},
            config={"region": "us-east-1"},
        )
        out = capsys.readouterr().out
        assert "region us-west-2 overrides" in out
        assert "us-east-1" in out

    def test_silent_when_cli_matches_config(self, capsys):
        deploy_mod._warn_conflicts(
            cli={"region": "us-east-1"},
            config={"region": "us-east-1"},
        )
        assert capsys.readouterr().out == ""

    def test_silent_when_cli_arg_not_provided(self, capsys):
        # CLI arg == None means the user didn't pass it — config wins,
        # no override happened, no warning.
        deploy_mod._warn_conflicts(
            cli={"region": None, "profile": None},
            config={"region": "us-east-1", "profile": "my-dev"},
        )
        assert capsys.readouterr().out == ""

    def test_silent_when_config_missing(self, capsys):
        # config doesn't have the key at all — CLI is the only source,
        # so no "override" to warn about.
        deploy_mod._warn_conflicts(
            cli={"region": "us-west-2"},
            config={},
        )
        assert capsys.readouterr().out == ""

    def test_warns_per_conflicting_field(self, capsys):
        deploy_mod._warn_conflicts(
            cli={"region": "us-west-2", "profile": "one-off"},
            config={"region": "us-east-1", "profile": "normal"},
        )
        out = capsys.readouterr().out
        assert "region us-west-2" in out
        assert "profile one-off" in out


# ─────────────────────────────────────────────────────────────────────
#  _print_deploy_summary
# ─────────────────────────────────────────────────────────────────────


class TestPrintDeploySummary:
    def test_prints_every_field_and_stack_hint(self, capsys):
        deploy_mod._print_deploy_summary(
            dag_id="daily_orders",
            stage="dev",
            stack_name="acme-dev",
            namespace="acme",
            region="us-east-1",
            profile="my-dev",
            pipeline_stack="acme-dev-polyris-daily_orders",
        )
        out = capsys.readouterr().out
        assert "daily_orders" in out
        assert "stage:" in out and "dev" in out
        assert "sam stack:" in out and "acme-dev" in out
        # The hint that ties the sam stack to CFN outputs is what makes the
        # summary *educational*, not just decorative — pin it.
        assert "CloudFormation outputs" in out
        assert "acme" in out and "us-east-1" in out
        assert "my-dev" in out
        assert "acme-dev-polyris-daily_orders" in out

    def test_shows_default_placeholder_when_profile_none(self, capsys):
        deploy_mod._print_deploy_summary(
            dag_id="x",
            stage="dev",
            stack_name="acme-dev",
            namespace="acme",
            region="us-east-1",
            profile=None,
            pipeline_stack="acme-dev-polyris-x",
        )
        assert "(default)" in capsys.readouterr().out


# ─────────────────────────────────────────────────────────────────────
#  _fail_no_stack_name
# ─────────────────────────────────────────────────────────────────────


class TestFailNoStackName:
    def test_exits_nonzero_with_actionable_message(self, capsys):
        with pytest.raises(SystemExit) as exc:
            deploy_mod._fail_no_stack_name("dev")
        assert exc.value.code == 1
        out = capsys.readouterr().out
        # The message must name the config key AND point at the alternative CLI
        # flag — otherwise the user is left guessing where to put the fix.
        assert "stack_name" in out
        assert "dev" in out
        assert "--stack" in out
        assert "samconfig.toml" in out

    def test_recommends_polyris_init_when_no_config_py_exists(self, mocker, capsys):
        # A brand-new project has no config.py yet; telling the user to "add
        # stack_name to your config.py" is useless when the file doesn't
        # exist. Point them at polyris-init instead.
        mocker.patch("polyris.config._find_project_config", return_value=None)
        with pytest.raises(SystemExit):
            deploy_mod._fail_no_stack_name("dev")
        out = capsys.readouterr().out
        assert "polyris-init --project" in out
        assert "--stack" in out  # still show the CLI-alternative escape hatch


# ─────────────────────────────────────────────────────────────────────
#  _read_sam_stack
# ─────────────────────────────────────────────────────────────────────


class TestReadSamStack:
    def test_returns_outputs_and_params_dicts(self, mocker):
        session = mocker.MagicMock()
        cfn = session.client.return_value
        cfn.describe_stacks.return_value = {
            "Stacks": [
                {
                    "Outputs": [
                        {"OutputKey": "DependencyWrapperArn", "OutputValue": "arn:wrap"},
                        {"OutputKey": "PipelineRegistryTable", "OutputValue": "reg"},
                    ],
                    "Parameters": [
                        {"ParameterKey": "Stage", "ParameterValue": "dev"},
                        {"ParameterKey": "Namespace", "ParameterValue": "acme"},
                    ],
                }
            ]
        }
        outputs, params = deploy_mod._read_sam_stack(session, "acme-dev")

        assert outputs == {
            "DependencyWrapperArn": "arn:wrap",
            "PipelineRegistryTable": "reg",
        }
        assert params == {"Stage": "dev", "Namespace": "acme"}
        session.client.assert_called_once_with("cloudformation")
        cfn.describe_stacks.assert_called_once_with(StackName="acme-dev")

    def test_missing_stack_exits_with_hint(self, mocker, capsys):
        from botocore.exceptions import ClientError

        session = mocker.MagicMock()
        cfn = session.client.return_value
        cfn.describe_stacks.side_effect = ClientError(
            {"Error": {"Code": "ValidationError",
                       "Message": "Stack with id acme-dev does not exist"}},
            "DescribeStacks",
        )

        with pytest.raises(SystemExit) as exc:
            deploy_mod._read_sam_stack(session, "acme-dev")
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "acme-dev" in out
        assert "sam deploy" in out            # tells the user the recovery
        assert "samconfig.toml" in out        # names the value to check

    def test_other_client_errors_propagate(self, mocker):
        from botocore.exceptions import ClientError

        session = mocker.MagicMock()
        cfn = session.client.return_value
        cfn.describe_stacks.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no perms"}},
            "DescribeStacks",
        )
        # Non-"does not exist" errors must not be swallowed (ADR #38): the
        # caller sees them.
        with pytest.raises(ClientError):
            deploy_mod._read_sam_stack(session, "acme-dev")

    def test_handles_stack_with_no_outputs_or_params(self, mocker):
        # A brand-new stack right after CREATE_IN_PROGRESS has neither
        # Outputs nor Parameters populated in describe_stacks. Guard against
        # a None-safe traversal so the reader doesn't crash on that state.
        session = mocker.MagicMock()
        cfn = session.client.return_value
        cfn.describe_stacks.return_value = {"Stacks": [{}]}
        outputs, params = deploy_mod._read_sam_stack(session, "acme-dev")
        assert outputs == {}
        assert params == {}


# ─────────────────────────────────────────────────────────────────────
#  SAM_STACK_OUTPUT_KEYS — pin the constant so a template rename is caught
# ─────────────────────────────────────────────────────────────────────


class TestSamStackOutputKeys:
    def test_lists_the_required_outputs(self):
        # If sam/template.yaml renames one of these, this test breaks and
        # forces the reader to be updated in the same change (Principle #13).
        assert deploy_mod.SAM_STACK_OUTPUT_KEYS == (
            "DependencyWrapperArn",
            "OrchestrationRoleArn",
            "PipelineRegistryTable",
            "PipelineTokensTable",
            "AssetSubscriptionsTable",
            "ResultsBucket",
        )
