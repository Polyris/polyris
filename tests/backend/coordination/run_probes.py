"""Apply each mutation probe, run its scenario, expect a failure, restore the files.

    python tests/backend/coordination/run_probes.py

Exit code 1 when a probe survives (its scenario still passes) or no longer
applies (one of its texts is gone from its file). A probe whose scenario waits
for an unbound stage (needs_binding) is skipped, as its scenario is.
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))

from probes import PROBES  # noqa: E402 — the harness directory is not a package
from scenario_marks import scenarios  # noqa: E402
from stages import BOUND_STAGES  # noqa: E402


def _waiting(scenario: str) -> list:
    """The unbound stages a probe's scenario waits for: the probe cannot run until they are bound."""
    stages = scenarios().get(scenario.split("[")[0], {}).get("needs_binding", [])
    return [s for s in stages if s not in BOUND_STAGES]


def main() -> int:
    problems = skipped = 0
    for probe in PROBES:
        waiting = _waiting(probe.scenario)
        if waiting:
            skipped += 1
            print(f"SKIPPED  {probe.defect}: {probe.scenario} (needs {', '.join(waiting)})")
            continue
        originals = {}
        try:
            for edit in probe.edits:
                path = REPO_ROOT / edit.path
                text = originals.setdefault(path, path.read_text()) if path not in originals else path.read_text()
                if text.count(edit.old) != 1:
                    raise LookupError(f"the text of an edit occurs {text.count(edit.old)} times in {edit.path}")
                path.write_text(text.replace(edit.old, edit.new))
            result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", probe.scenario],
                                    cwd=REPO_ROOT, capture_output=True, text=True)
            # pytest's 1 is a failed test. Any other code (an error while collecting, an interrupt, no test
            # found) means the scenario never judged the probe, which is no catch (the review of TR-4).
            caught = result.returncode == 1
            problems += not caught
            verdict = "CAUGHT  " if caught else ("SURVIVED" if result.returncode == 0 else f"NOT RUN (exit {result.returncode})")
            print(f"{verdict} {probe.defect}: {probe.scenario}")
        except LookupError as exc:
            problems += 1
            print(f"STALE    {probe.defect}: {exc}")
        finally:
            for path, text in originals.items():
                path.write_text(text)
    print(f"{len(PROBES)} probes, {skipped} skipped until their stage, {problems} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
