"""_shared Lambda constants parity check (v0.80.0, ADR #83).

`sam/lambdas/_shared/constants.py` (copied to evaluate_deps) keeps a few
status classes (TaskStatus, TriggerRule, AssetOperator) defined manually
rather than re-exported from the generated module, so the file works
standalone in unit tests (it has an ImportError fallback for the generated
status sets). That manual copy is otherwise unguarded — the enum drift
check (check-generate-enums) only validates the generated mirrors, and the
sync-constants diff only proves _shared == evaluate_deps.

This checker closes that gap: it verifies every value the manual _shared
classes define also exists in the canonical polyris/constants.py, so the
_shared copy can never silently diverge from the single source of truth.
It does NOT require _shared to define every canonical value (canonical may
be a superset, e.g. TaskStatus.SUCCEEDED) — only that _shared introduces
nothing the canonical source doesn't have.

Run via: `make sync-constants`. Exits 1 on drift.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Dict, List, Set

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SHARED = REPO_ROOT / "sam" / "lambdas" / "_shared" / "constants.py"


def _load_shared_module():
    """Import _shared/constants.py standalone (its generated-sets import
    falls back gracefully when constants_generated isn't on the path).

    Loads in isolation: the temporary sys.path entry needed to resolve the
    file, and any bare-named modules its import pulls in (constants_generated,
    etc.), are reverted before returning. Otherwise this helper would leak
    `sam/lambdas/_shared` onto sys.path for the rest of the process — which
    shadows console_api's same-named modules (`constants`, `constants_generated`)
    in a shared pytest run and breaks unrelated tests (e.g. test_templates'
    `from constants import Limits`). The returned module object stays valid
    regardless of the sys.modules cleanup."""
    shared_dir = str(SHARED.parent)
    saved_path = list(sys.path)
    saved_modules = set(sys.modules)
    sys.path.insert(0, shared_dir)
    try:
        spec = importlib.util.spec_from_file_location("_shared_constants", SHARED)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path[:] = saved_path
        for _name in set(sys.modules) - saved_modules:
            sys.modules.pop(_name, None)


def _class_values(cls) -> Set[str]:
    return {
        v for k, v in vars(cls).items()
        if not k.startswith("_") and isinstance(v, str)
    }


def check_shared_constants() -> int:
    """Verify _shared manual status classes are a subset of canonical."""
    from polyris import constants as canon

    sh = _load_shared_module()

    # (shared class, canonical value set) — canonical may be a superset.
    families: Dict[str, Set[str]] = {
        "TaskStatus": {m.value for m in canon.TaskStatus},
        "TriggerRule": _class_values(canon.TriggerRule),
        "AssetOperator": _class_values(canon.AssetOperator),
    }

    problems: List[str] = []
    for name, canon_vals in families.items():
        sh_cls = getattr(sh, name, None)
        if sh_cls is None:
            continue  # _shared no longer defines it (consolidated) — fine
        extra = _class_values(sh_cls) - canon_vals
        if extra:
            problems.append(
                f"_shared {name} defines value(s) not in canonical "
                f"polyris/constants.py: {sorted(extra)}. Update the canonical "
                f"source or remove the stray value from _shared."
            )

    if problems:
        print("[shared-constants] DRIFT from canonical:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print("  ✅ _shared status classes are consistent with canonical")
    return 0


if __name__ == "__main__":
    sys.exit(check_shared_constants())
