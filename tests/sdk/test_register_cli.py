"""Tests for polyris/register.py — register pipeline CLI.

Removes register.py from the coverage omit list (pyproject.toml). Every logic
branch is covered here. The boto3 boundary is mocked at polyris.register.boto3;
get_sfn_client is mocked for higher-level register_pipeline / find_sfn_arn tests.
"""
import json

import pytest
from botocore.exceptions import ClientError, ProfileNotFound

from polyris import register


# ─── helpers ──────────────────────────────────────────────────────────────────

_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:my-pipeline"


def _client_error(code: str = "SomeError") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "msg"}}, "op")


def _sfn_mock(mocker, *, does_not_exist: bool = False, already_exists: bool = False,
              client_error: bool = False):
    """Mock SFN client with exception classes wired up."""
    class _DoesNotExist(Exception):
        pass

    class _AlreadyExists(Exception):
        pass

    from datetime import datetime, timezone

    sfn = mocker.MagicMock()
    sfn.exceptions.StateMachineDoesNotExist = _DoesNotExist
    sfn.exceptions.ExecutionAlreadyExists = _AlreadyExists

    if does_not_exist:
        sfn.start_execution.side_effect = _DoesNotExist()
    elif already_exists:
        sfn.start_execution.side_effect = _AlreadyExists()
    elif client_error:
        sfn.start_execution.side_effect = _client_error()
    else:
        sfn.start_execution.return_value = {
            "executionArn": "arn:aws:states:us-east-1:123:execution:my-pipeline:exec-1",
            "startDate": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
    return sfn


# ─── get_sfn_client ────────────────────────────────────────────────────────────

class TestGetSfnClient:
    def test_default_credentials(self, mocker, monkeypatch):
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        boto = mocker.patch("polyris.register.boto3")
        register.get_sfn_client("us-east-1")
        boto.client.assert_called_once_with("stepfunctions", region_name="us-east-1")

    def test_profile_only(self, mocker, monkeypatch):
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        boto = mocker.patch("polyris.register.boto3")
        register.get_sfn_client("us-east-1", profile="myprofile")
        boto.Session.assert_called_once_with(profile_name="myprofile", region_name="us-east-1")
        boto.Session.return_value.client.assert_called_once_with("stepfunctions")

    def test_aws_profile_env_fallback(self, mocker, monkeypatch):
        monkeypatch.setenv("AWS_PROFILE", "envprofile")
        boto = mocker.patch("polyris.register.boto3")
        register.get_sfn_client("us-east-1")
        boto.Session.assert_called_once_with(profile_name="envprofile", region_name="us-east-1")

    def test_role_arn_no_profile(self, mocker, monkeypatch):
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        boto = mocker.patch("polyris.register.boto3")
        boto.client.return_value.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "AKI",
                "SecretAccessKey": "SAK",
                "SessionToken": "ST",
            }
        }
        register.get_sfn_client("us-east-1", role_arn="arn:aws:iam::123:role/r")
        # First call: sts client; second call: stepfunctions client with creds
        assert boto.client.call_count == 2

    def test_role_arn_with_profile(self, mocker, monkeypatch):
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        boto = mocker.patch("polyris.register.boto3")
        boto.Session.return_value.client.return_value.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "AKI",
                "SecretAccessKey": "SAK",
                "SessionToken": "ST",
            }
        }
        register.get_sfn_client("us-east-1", profile="myprofile",
                                 role_arn="arn:aws:iam::123:role/r")
        boto.Session.assert_called_once_with(profile_name="myprofile", region_name="us-east-1")
        boto.client.assert_called_once()  # the final stepfunctions client

    def test_profile_not_found_exits(self, mocker, capsys):
        boto = mocker.patch("polyris.register.boto3")
        boto.Session.side_effect = ProfileNotFound(profile="bad-profile")
        with pytest.raises(SystemExit) as exc:
            register.get_sfn_client("us-east-1", profile="bad-profile")
        assert exc.value.code == 1
        captured = capsys.readouterr()
        # Diagnostic goes to stderr so `--json` stdout stays parseable.
        assert "bad-profile" in captured.err
        assert captured.out == ""


# ─── register_pipeline ─────────────────────────────────────────────────────────

class TestRegisterPipeline:
    def test_success_returns_execution_details(self, mocker):
        sfn = _sfn_mock(mocker)
        mocker.patch("polyris.register.get_sfn_client", return_value=sfn)
        result = register.register_pipeline(_ARN)
        assert result["success"] is True
        assert result["execution_arn"].startswith("arn:")
        assert "2026" in result["started_at"]

    def test_region_extracted_from_arn(self, mocker):
        sfn = _sfn_mock(mocker)
        patch = mocker.patch("polyris.register.get_sfn_client", return_value=sfn)
        register.register_pipeline(_ARN)
        patch.assert_called_once_with("us-east-1", None, None)

    def test_region_fallback_on_malformed_arn(self, mocker):
        sfn = _sfn_mock(mocker)
        patch = mocker.patch("polyris.register.get_sfn_client", return_value=sfn)
        register.register_pipeline("not-an-arn")
        patch.assert_called_once_with("us-east-1", None, None)

    def test_region_explicit_overrides_arn(self, mocker):
        sfn = _sfn_mock(mocker)
        patch = mocker.patch("polyris.register.get_sfn_client", return_value=sfn)
        register.register_pipeline(_ARN, region="eu-west-1")
        patch.assert_called_once_with("eu-west-1", None, None)

    def test_state_machine_does_not_exist(self, mocker):
        sfn = _sfn_mock(mocker, does_not_exist=True)
        mocker.patch("polyris.register.get_sfn_client", return_value=sfn)
        result = register.register_pipeline(_ARN)
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_execution_already_exists_is_no_op(self, mocker):
        sfn = _sfn_mock(mocker, already_exists=True)
        mocker.patch("polyris.register.get_sfn_client", return_value=sfn)
        result = register.register_pipeline(_ARN)
        assert result["success"] is True
        assert result["execution_arn"] == ""

    def test_client_error_returns_failure(self, mocker):
        sfn = _sfn_mock(mocker, client_error=True)
        mocker.patch("polyris.register.get_sfn_client", return_value=sfn)
        result = register.register_pipeline(_ARN)
        assert result["success"] is False
        assert "error" in result


# ─── find_sfn_arn ──────────────────────────────────────────────────────────────

class TestFindSfnArn:
    def _page(self, names):
        return {
            "stateMachines": [
                {"name": n, "stateMachineArn": f"arn:aws:states:us-east-1:123:stateMachine:{n}"}
                for n in names
            ]
        }

    def _mock_paginator(self, mocker, names):
        sfn = mocker.MagicMock()
        sfn.get_paginator.return_value.paginate.return_value = [self._page(names)]
        mocker.patch("polyris.register.get_sfn_client", return_value=sfn)
        return sfn

    def test_exact_name_match(self, mocker):
        self._mock_paginator(mocker, ["my-pipeline"])
        result = register.find_sfn_arn("my-pipeline", "us-east-1")
        assert result == "arn:aws:states:us-east-1:123:stateMachine:my-pipeline"

    def test_suffix_match(self, mocker):
        self._mock_paginator(mocker, ["acme-prod-my-pipeline"])
        result = register.find_sfn_arn("my-pipeline", "us-east-1")
        assert result == "arn:aws:states:us-east-1:123:stateMachine:acme-prod-my-pipeline"

    def test_namespace_filter_skips_wrong_namespace(self, mocker):
        self._mock_paginator(mocker, ["team-b-my-pipeline"])
        result = register.find_sfn_arn("my-pipeline", "us-east-1", namespace="team-a")
        assert result is None

    def test_namespace_filter_accepts_correct_namespace(self, mocker):
        self._mock_paginator(mocker, ["team-a-my-pipeline"])
        result = register.find_sfn_arn("my-pipeline", "us-east-1", namespace="team-a")
        assert result == "arn:aws:states:us-east-1:123:stateMachine:team-a-my-pipeline"

    def test_no_match_returns_none(self, mocker):
        self._mock_paginator(mocker, ["other-pipeline"])
        result = register.find_sfn_arn("my-pipeline", "us-east-1")
        assert result is None

    def test_client_error_propagates(self, mocker):
        """AccessDenied / throttling / service outage is NOT the same as
        "pipeline not found". Caller must be able to distinguish."""
        sfn = mocker.MagicMock()
        sfn.get_paginator.side_effect = _client_error("AccessDeniedException")
        mocker.patch("polyris.register.get_sfn_client", return_value=sfn)
        with pytest.raises(ClientError):
            register.find_sfn_arn("my-pipeline", "us-east-1")


# ─── main ─────────────────────────────────────────────────────────────────────

class TestMain:
    def _success(self, mocker, *, execution_arn="arn:...exec-1"):
        return mocker.patch(
            "polyris.register.register_pipeline",
            return_value={
                "success": True,
                "execution_arn": execution_arn,
                "started_at": "2026-01-01T00:00:00+00:00",
            },
        )

    def test_register_by_arn(self, mocker, capsys):
        self._success(mocker)
        mocker.patch("sys.argv", ["polyris-register", _ARN])
        register.main()
        assert "✅" in capsys.readouterr().out

    def test_register_by_name_finds_and_registers(self, mocker, capsys):
        mocker.patch("polyris.register.find_sfn_arn", return_value=_ARN)
        self._success(mocker)
        mocker.patch("sys.argv", ["polyris-register", "--name", "my-pipeline",
                                   "--region", "us-east-1"])
        register.main()
        out = capsys.readouterr().out
        assert "Found" in out
        assert "✅" in out

    def test_name_not_found_exits(self, mocker, capsys):
        mocker.patch("polyris.register.find_sfn_arn", return_value=None)
        mocker.patch("sys.argv", ["polyris-register", "--name", "missing",
                                   "--region", "us-east-1"])
        with pytest.raises(SystemExit) as exc:
            register.main()
        assert exc.value.code == 1
        assert "missing" in capsys.readouterr().out

    def test_name_search_access_denied_shows_real_error(self, mocker, capsys):
        """AccessDenied on the paginator must not masquerade as "pipeline not
        found" — operator needs to know they are missing an IAM permission,
        not chasing a non-existent pipeline."""
        mocker.patch(
            "polyris.register.find_sfn_arn",
            side_effect=_client_error("AccessDeniedException"),
        )
        mocker.patch("sys.argv", ["polyris-register", "--name", "my-pipeline",
                                   "--region", "us-east-1"])
        with pytest.raises(SystemExit) as exc:
            register.main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "AccessDeniedException" in out
        assert "not found" not in out.lower()

    def test_name_search_access_denied_json_mode(self, mocker, capsys):
        mocker.patch(
            "polyris.register.find_sfn_arn",
            side_effect=_client_error("AccessDeniedException"),
        )
        mocker.patch("sys.argv", ["polyris-register", "--json", "--name", "p",
                                   "--region", "us-east-1"])
        with pytest.raises(SystemExit) as exc:
            register.main()
        assert exc.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data["success"] is False
        assert "AccessDeniedException" in data["error"]

    def test_no_arn_no_name_exits(self, mocker):
        mocker.patch("sys.argv", ["polyris-register"])
        with pytest.raises(SystemExit) as exc:
            register.main()
        assert exc.value.code == 1

    def test_no_arn_no_name_json_mode_emits_clean_json(self, mocker, capsys):
        """--json must keep stdout parseable even when no ARN/--name provided.
        Help goes to stderr; stdout gets {success:false, error:...}."""
        mocker.patch("sys.argv", ["polyris-register", "--json"])
        with pytest.raises(SystemExit) as exc:
            register.main()
        assert exc.value.code == 1
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["success"] is False
        assert "error" in data
        # Help text is useful for interactive callers; stderr keeps stdout clean.
        assert "usage" in captured.err.lower()

    def test_already_registered_prints_no_op(self, mocker, capsys):
        mocker.patch(
            "polyris.register.register_pipeline",
            return_value={"success": True, "execution_arn": "", "started_at": ""},
        )
        mocker.patch("sys.argv", ["polyris-register", _ARN])
        register.main()
        assert "already" in capsys.readouterr().out.lower()

    def test_failure_exits(self, mocker, capsys):
        mocker.patch(
            "polyris.register.register_pipeline",
            return_value={"success": False, "error": "AccessDenied"},
        )
        mocker.patch("sys.argv", ["polyris-register", _ARN])
        with pytest.raises(SystemExit) as exc:
            register.main()
        assert exc.value.code == 1
        assert "AccessDenied" in capsys.readouterr().out

    def test_json_success_prints_dict(self, mocker, capsys):
        self._success(mocker)
        mocker.patch("sys.argv", ["polyris-register", "--json", _ARN])
        register.main()
        data = json.loads(capsys.readouterr().out)
        assert data["success"] is True

    def test_json_failure_exits(self, mocker, capsys):
        mocker.patch(
            "polyris.register.register_pipeline",
            return_value={"success": False, "error": "boom"},
        )
        mocker.patch("sys.argv", ["polyris-register", "--json", _ARN])
        with pytest.raises(SystemExit) as exc:
            register.main()
        assert exc.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data["success"] is False

    def test_json_by_name_stdout_is_pure_json(self, mocker, capsys, monkeypatch):
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        sfn = mocker.MagicMock()
        sfn.get_paginator.return_value.paginate.return_value = [
            {"stateMachines": [{"name": "my-pipeline", "stateMachineArn": _ARN}]}
        ]
        mocker.patch("polyris.register.boto3").client.return_value = sfn
        self._success(mocker)
        mocker.patch("sys.argv", ["polyris-register", "--json", "--name",
                                   "my-pipeline", "--region", "us-east-1"])
        register.main()
        data = json.loads(capsys.readouterr().out)
        assert data["success"] is True

    def test_json_name_not_found_emits_json_error(self, mocker, capsys, monkeypatch):
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        sfn = mocker.MagicMock()
        sfn.get_paginator.return_value.paginate.return_value = [{"stateMachines": []}]
        mocker.patch("polyris.register.boto3").client.return_value = sfn
        mocker.patch("sys.argv", ["polyris-register", "--json", "--name",
                                   "missing", "--region", "us-east-1"])
        with pytest.raises(SystemExit) as exc:
            register.main()
        assert exc.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data["success"] is False
        assert "missing" in data["error"]

    def test_profile_flag_printed(self, mocker, capsys):
        self._success(mocker)
        mocker.patch("sys.argv", ["polyris-register", "--profile", "myprofile", _ARN])
        register.main()
        assert "myprofile" in capsys.readouterr().out

    def test_aws_profile_env_printed(self, mocker, capsys, monkeypatch):
        monkeypatch.setenv("AWS_PROFILE", "envprofile")
        self._success(mocker)
        mocker.patch("sys.argv", ["polyris-register", _ARN])
        register.main()
        assert "envprofile" in capsys.readouterr().out

    def test_role_arn_printed(self, mocker, capsys):
        self._success(mocker)
        mocker.patch("sys.argv", [
            "polyris-register", "--role-arn", "arn:aws:iam::123:role/r", _ARN,
        ])
        register.main()
        assert "arn:aws:iam::123:role/r" in capsys.readouterr().out
