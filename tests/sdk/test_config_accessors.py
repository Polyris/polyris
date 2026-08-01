"""Config accessor tests — profile/reload/reset and the _StageConfig view.

Covers the remaining ``PolyrisConfig`` / ``_StageConfig`` accessors (CLAUDE.md
#13) by injecting ``_environments`` directly (bypassing the file loader) and
clearing the relevant env vars so the config-file fallbacks are exercised.
"""
from __future__ import annotations

from polyris.config import PolyrisConfig, _StageConfig, _RolesDict


def _cfg(environments, default_stage="dev"):
    c = PolyrisConfig.__new__(PolyrisConfig)  # bypass file-loading __init__
    c._environments = environments
    c._default_stage = default_stage
    return c


def _clear_profile_env(monkeypatch):
    for var in ("POLYRIS_PROFILE", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)


class TestPolyrisConfigAccessors:
    def test_profile_falls_back_to_env_config(self, monkeypatch):
        _clear_profile_env(monkeypatch)
        c = _cfg({"dev": {"profile": "data-eng"}})
        assert c.profile == "data-eng"

    def test_profile_prefers_env_var(self, monkeypatch):
        monkeypatch.setenv("POLYRIS_PROFILE", "env-prof")
        c = _cfg({"dev": {"profile": "ignored"}})
        assert c.profile == "env-prof"

    def test_reload_repopulates_from_project(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # no config.py here → empty project
        c = _cfg({"stale": {"x": 1}}, default_stage="stale")
        c.reload()
        assert c._environments == {}
        assert c._default_stage == "dev"

    def test_reset_clears_state(self):
        c = _cfg({"dev": {"a": 1}}, default_stage="prod")
        c.reset()
        assert c._environments == {}
        assert c._default_stage == "dev"
        assert PolyrisConfig._loaded is False


class TestStageConfigView:
    def _stage(self, environments, stage="dev"):
        parent = _cfg(environments, default_stage=stage)
        return _StageConfig(parent, stage)

    def test_profile_falls_back_to_env_config(self, monkeypatch):
        _clear_profile_env(monkeypatch)
        sc = self._stage({"dev": {"profile": "stage-prof"}})
        assert sc.profile == "stage-prof"

    def test_roles_returns_roles_dict(self):
        sc = self._stage({"dev": {"roles": {"runner": "arn:aws:iam::1:role/r"}}})
        roles = sc.roles
        assert isinstance(roles, _RolesDict)
        assert roles["runner"] == "arn:aws:iam::1:role/r"

    def test_get_reads_env_config_with_default(self, monkeypatch):
        monkeypatch.delenv("POLYRIS_BUCKET", raising=False)
        sc = self._stage({"dev": {"bucket": "my-bucket"}})
        assert sc.get("bucket") == "my-bucket"
        assert sc.get("absent", "fallback") == "fallback"
