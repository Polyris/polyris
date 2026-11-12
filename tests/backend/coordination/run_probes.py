"""Apply each mutation probe, run its scenario, expect a failure, restore the file.

    python tests/backend/coordination/run_probes.py

Exit code 1 when a probe survives (its scenario still passes) or no longer
applies (its text is gone from the file).
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))

from probes import PROBES  # noqa: E402 — the harness directory is not a package


def main() -> int:
    failures = 0
    for probe in PROBES:
        path = REPO_ROOT / probe.path
        original = path.read_text()
        if original.count(probe.old) != 1:
            print(f"STALE   {probe.defect}: the probe text occurs {original.count(probe.old)} times in {probe.path}")
            failures += 1
            continue
        try:
            path.write_text(original.replace(probe.old, probe.new))
            result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", probe.scenario],
                                    cwd=REPO_ROOT, capture_output=True, text=True)
        finally:
            path.write_text(original)
        caught = result.returncode != 0
        failures += not caught
        print(f"{'CAUGHT ' if caught else 'SURVIVED'} {probe.defect}: {probe.scenario}")
    print(f"{len(PROBES)} probes, {failures} problems")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
