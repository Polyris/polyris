"""Apply each mutation probe, run its scenario, expect a failure, restore the files.

    python tests/backend/coordination/run_probes.py

Exit code 1 when a probe survives (its scenario still passes) or no longer
applies (one of its texts is gone from its file).
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))

from probes import PROBES  # noqa: E402 — the harness directory is not a package


def main() -> int:
    problems = 0
    for probe in PROBES:
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
            caught = result.returncode != 0
            problems += not caught
            print(f"{'CAUGHT  ' if caught else 'SURVIVED'} {probe.defect}: {probe.scenario}")
        except LookupError as exc:
            problems += 1
            print(f"STALE    {probe.defect}: {exc}")
        finally:
            for path, text in originals.items():
                path.write_text(text)
    print(f"{len(PROBES)} probes, {problems} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
