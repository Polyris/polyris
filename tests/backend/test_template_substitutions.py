"""Every ${placeholder} in the run_task SFN template must be provided by the
state machine's DefinitionSubstitutions. A missing one deploys as a literal and
fails at runtime (it hid a real pipeline_registry_table bug on the failure path).
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_TPL = _ROOT / "sam" / "sfn_templates" / "helpers" / "run_task" / "sfn.tpl.json"
_YAML = _ROOT / "sam" / "template.yaml"


def _run_task_substitutions() -> set:
    lines = _YAML.read_text().splitlines()
    uri = next(i for i, ln in enumerate(lines) if "run_task/sfn.tpl.json" in ln)
    start = next(i for i in range(uri, 0, -1) if "DefinitionSubstitutions:" in lines[i])
    keys = set()
    for line in lines[start + 1:uri]:
        m = re.match(r"\s+([a-z_]+):", line)
        if m:
            keys.add(m.group(1))
    return keys


def test_all_run_task_placeholders_are_substituted():
    used = set(re.findall(r"\$\{([a-z_]+)\}", _TPL.read_text()))
    provided = _run_task_substitutions()
    missing = used - provided
    assert not missing, f"run_task template uses unsubstituted placeholders: {sorted(missing)}"
