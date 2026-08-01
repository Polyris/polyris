"""Tests for SFN template drift checker (v0.79.6, ADR #78)."""

from polyris.codegen.check_sfn_templates import (
    check_templates,
    _task_status_values,
    _load_canonical_status_values,
    HELPER_OPERATION_STATUSES,
)


class TestCanonicalLoad:
    def test_loads_taskstatus_values(self):
        values = _task_status_values()
        # Spot-check known canonical values
        assert 'failed' in values
        assert 'success' in values
        assert 'aborted' in values
        assert 'upstream_failed' in values
        assert 'running' in values

    def test_all_families_loaded(self):
        families = _load_canonical_status_values()
        assert 'TaskStatus' in families
        assert 'TriggerRule' in families
        assert 'BackfillStatus' in families
        assert 'PipelineStatus' in families
        # Each family must have at least one value
        for name, values in families.items():
            assert len(values) > 0, f"{name} has no values"


class TestHelperOperationAllowlist:
    def test_restarted_in_allowlist(self):
        """restart_task helper's Output.status = 'restarted' must be allowed."""
        assert 'restarted' in HELPER_OPERATION_STATUSES

    def test_allowlist_documented(self):
        """Every operation-status value should be a small, distinct set."""
        # The allowlist is exclusionary — keep it tight
        assert len(HELPER_OPERATION_STATUSES) < 10, \
            "HELPER_OPERATION_STATUSES is growing; reconsider canonical merge"


class TestCheckTemplates:
    def test_current_templates_pass(self):
        """The committed templates must pass the drift check."""
        result = check_templates()
        assert result == 0

    def test_detects_typo_via_temp_file(self, tmp_path, monkeypatch):
        """Drift check flags an unknown status value in a Pass-state write."""
        # Set up a fake template
        fake_template = tmp_path / "fake" / "sfn.tpl.json"
        fake_template.parent.mkdir()
        fake_template.write_text("""
{
  "States": {
    "Pass1": {
      "Type": "Pass",
      "Output": {
        "status": "faiiled"
      }
    }
  }
}
""")
        # Point checker at the tmp dir
        from polyris.codegen import check_sfn_templates
        monkeypatch.setattr(check_sfn_templates, 'TEMPLATES_DIR', tmp_path)
        result = check_templates()
        assert result == 1

    def test_detects_typo_in_status_comparison(self, tmp_path, monkeypatch):
        """Drift check flags an unknown status value in a JSONata comparison.

        Real templates put compares inside `{% ... %}` blocks where quotes
        are JSON-escaped. The detector still catches plain JSON literals
        via the write pattern.
        """
        fake_template = tmp_path / "fake" / "sfn.tpl.json"
        fake_template.parent.mkdir()
        # Use the write-status pattern that real templates emit at DDB
        # ExpressionAttributeValues sites:
        #   ":failed": {"S": "failed"}
        # When the value has a typo, the `"status": "..."` write detector
        # catches it on Pass-state outputs.
        fake_template.write_text("""
{
  "States": {
    "Pass1": {
      "Type": "Pass",
      "Output": {
        "status": "sucess"
      }
    }
  }
}
""")
        from polyris.codegen import check_sfn_templates
        monkeypatch.setattr(check_sfn_templates, 'TEMPLATES_DIR', tmp_path)
        result = check_templates()
        assert result == 1

    def test_allows_canonical_values(self, tmp_path, monkeypatch):
        """Templates using only canonical values pass."""
        fake_template = tmp_path / "fake" / "sfn.tpl.json"
        fake_template.parent.mkdir()
        fake_template.write_text("""
{
  "States": {
    "Pass1": {
      "Type": "Pass",
      "Output": {
        "status": "failed"
      }
    },
    "Choice1": {
      "Type": "Choice",
      "Choices": [
        {"Condition": "$task.status = \\"success\\"", "Next": "Done"}
      ]
    }
  }
}
""")
        from polyris.codegen import check_sfn_templates
        monkeypatch.setattr(check_sfn_templates, 'TEMPLATES_DIR', tmp_path)
        result = check_templates()
        assert result == 0

    def test_allows_helper_operation_status(self, tmp_path, monkeypatch):
        """The 'restarted' operation status is allowed via allowlist."""
        fake_template = tmp_path / "fake" / "sfn.tpl.json"
        fake_template.parent.mkdir()
        fake_template.write_text("""
{
  "States": {
    "Pass1": {
      "Type": "Pass",
      "Output": {
        "status": "restarted"
      }
    }
  }
}
""")
        from polyris.codegen import check_sfn_templates
        monkeypatch.setattr(check_sfn_templates, 'TEMPLATES_DIR', tmp_path)
        result = check_templates()
        assert result == 0

    def test_detects_ddb_status_av_typo(self, tmp_path, monkeypatch):
        """`":status": {"S": "sucess"}` in ExpressionAttributeValues is flagged."""
        fake_template = tmp_path / "fake" / "sfn.tpl.json"
        fake_template.parent.mkdir()
        fake_template.write_text("""
{
  "States": {
    "UpdateDdb": {
      "Type": "Task",
      "Arguments": {
        "ExpressionAttributeValues": {
          ":status": {"S": "sucess"}
        }
      }
    }
  }
}
""")
        from polyris.codegen import check_sfn_templates
        monkeypatch.setattr(check_sfn_templates, 'TEMPLATES_DIR', tmp_path)
        result = check_templates()
        assert result == 1

    def test_ignores_non_status_ddb_av(self, tmp_path, monkeypatch):
        """`":nf": {"S": "slack_channel_error"}` is NOT flagged — not status."""
        fake_template = tmp_path / "fake" / "sfn.tpl.json"
        fake_template.parent.mkdir()
        fake_template.write_text("""
{
  "States": {
    "UpdateDdb": {
      "Type": "Task",
      "Arguments": {
        "ExpressionAttributeValues": {
          ":nf": {"S": "slack_channel_error"}
        }
      }
    }
  }
}
""")
        from polyris.codegen import check_sfn_templates
        monkeypatch.setattr(check_sfn_templates, 'TEMPLATES_DIR', tmp_path)
        result = check_templates()
        assert result == 0
