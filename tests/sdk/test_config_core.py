"""Config tests — roles dict, PolyrisConfig accessors, stage views.

Covers ``polyris.config`` (CLAUDE.md #13). The singleton's file loading is
bypassed by injecting ``_environments`` directly after construction, so the
accessor logic (env-var override → stage config → default) is tested
deterministically without a real ``config.py`` on disk. Environment-override
branches use ``monkeypatch`` so they auto-restore.
"""
from __future__ import annotations

import pytest

from polyris.config import PolyrisConfig, _RolesDict, _StageConfig


def _cfg(environments=None, default_stage="dev"):
    """A PolyrisConfig with injected environments (no disk load)."""
    c = PolyrisConfig()
    c._environments = environments or {}
    c._default_stage = default_stage
    PolyrisConfig._loaded = True
    return c


# ============================================================ #
# _RolesDict
# ============================================================ #
class TestRolesDict:
    def test_lookup_from_dict(self):
        assert _RolesDict({"exec": "arn:role/exec"})["exec"] == "arn:role/exec"

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("POLYRIS_ROLE_EXEC", "arn:env/exec")
        assert _RolesDict({"exec": "arn:cfg/exec"})["exec"] == "arn:env/exec"

    def test_missing_role_raises_keyerror(self):
        with pytest.raises(KeyError):
            _RolesDict({})["nope"]

    def test_get_returns_default_on_missing(self):
        assert _RolesDict({}).get("nope", "fallback") == "fallback"

    def test_get_returns_value_when_present(self):
        assert _RolesDict({"x": "arn:x"}).get("x") == "arn:x"

    def test_contains_finds_key_in_dict(self):
        assert "exec" in _RolesDict({"exec": "arn:role/exec"})

    def test_contains_returns_false_for_missing_key(self):
        assert "exec" not in _RolesDict({})

    def test_contains_finds_env_var_override(self, monkeypatch):
        """B-75: `in` operator used to call __getitem__ via iteration fallback,
        missing env-var-only roles that have no entry in the backing dict."""
        monkeypatch.setenv("POLYRIS_ROLE_EXEC", "arn:env/exec")
        assert "exec" in _RolesDict({})

    def test_contains_non_string_key_returns_false(self):
        assert 42 not in _RolesDict({"x": "arn:x"})


# ============================================================ #
# PolyrisConfig accessors
# ============================================================ #
class TestPolyrisConfig:
    def test_namespace_from_stage_config(self):
        assert _cfg({"dev": {"namespace": "myproj"}}).namespace == "myproj"

    def test_namespace_default(self):
        assert _cfg({}).namespace == "polyris"

    def test_namespace_env_override(self, monkeypatch):
        monkeypatch.setenv("POLYRIS_NAMESPACE", "envns")
        assert _cfg({"dev": {"namespace": "cfgns"}}).namespace == "envns"

    def test_stage_default_and_override(self, monkeypatch):
        assert _cfg(default_stage="prod").stage == "prod"
        monkeypatch.setenv("POLYRIS_STAGE", "staging")
        assert _cfg(default_stage="prod").stage == "staging"

    def test_region_default_config_and_env(self, monkeypatch):
        assert _cfg({}).region == "us-east-1"
        assert _cfg({"dev": {"region": "eu-west-1"}}).region == "eu-west-1"
        monkeypatch.setenv("AWS_REGION", "ap-south-1")
        assert _cfg({"dev": {"region": "eu-west-1"}}).region == "ap-south-1"

    def test_profile_default_and_config(self):
        assert _cfg({}).profile is None
        assert _cfg({"dev": {"profile": "myprofile"}}).profile == "myprofile"

    def test_roles_accessor(self):
        roles = _cfg({"dev": {"roles": {"exec": "arn:r"}}}).roles
        assert isinstance(roles, _RolesDict)
        assert roles["exec"] == "arn:r"

    def test_get_with_default_and_env(self, monkeypatch):
        c = _cfg({"dev": {"foo": "bar"}})
        assert c.get("foo") == "bar"
        assert c.get("absent", "dflt") == "dflt"
        monkeypatch.setenv("POLYRIS_FOO", "fromenv")
        assert _cfg({"dev": {"foo": "bar"}}).get("foo") == "fromenv"

    def test_singleton_identity(self):
        assert PolyrisConfig() is PolyrisConfig()


# ============================================================ #
# _StageConfig (via for_stage)
# ============================================================ #
class TestStageConfig:
    def test_for_stage_returns_stage_view(self):
        view = _cfg({"prod": {"namespace": "pns", "region": "us-west-2"}}).for_stage("prod")
        assert isinstance(view, _StageConfig)
        assert view.stage == "prod"
        assert view.namespace == "pns"
        assert view.region == "us-west-2"

    def test_stage_view_namespace_default(self):
        view = _cfg({}).for_stage("prod")
        assert view.namespace == "polyris"

    def test_unknown_stage_with_configured_environments_raises(self):
        """--stage prd when only prod is configured must raise immediately,
        before any AWS call — not silently deploy to namespace='polyris'."""
        with pytest.raises(ValueError, match="prd"):
            _cfg({"prod": {"namespace": "pns"}}).for_stage("prd").namespace

    def test_unknown_stage_with_no_environments_uses_defaults(self):
        """No config.py at all (empty environments) must stay quiet — the user
        hasn't configured any stages yet, so any stage name is valid."""
        view = _cfg({}).for_stage("anything")
        assert view.namespace == "polyris"
