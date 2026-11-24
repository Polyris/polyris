"""The project's Claude Code hooks run from any directory the shell stands in (docs/tools/AGENTIC_WORKFLOW.md).

A hook named its script by a relative path: with the shell in ``ui/`` or a Lambda's folder it failed with exit 127,
and the destructive-command guard did not run for those commands (TR-6).
"""

import json
from pathlib import Path

SETTINGS = Path(__file__).resolve().parents[2] / ".claude" / "settings.json"


def _commands():
    hooks = json.loads(SETTINGS.read_text()).get("hooks", {})
    return [hook["command"] for groups in hooks.values() for group in groups for hook in group.get("hooks", [])
            if hook.get("type") == "command"]


def test_every_hook_names_its_script_from_the_project_directory():
    commands = _commands()
    assert commands, "no hook found: the scan of .claude/settings.json is broken"
    for command in commands:
        assert command.startswith('bash "$CLAUDE_PROJECT_DIR/'), command
