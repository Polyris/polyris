"""The optional `ee` overlay import in main.py (Principle #44).

Only the absence of `ee` itself means "free build". A present overlay that
fails to import — a missing dependency, or a name its imports no longer find —
must fail the Lambda loudly instead of silently dropping every paid route.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

MAIN = Path(__file__).resolve().parent.parent / "main.py"


def _load_main():
    spec = importlib.util.spec_from_file_location("main_import_guard_probe", MAIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _overlay(monkeypatch, tmp_path, init: str) -> None:
    """Make `import ee` find a package whose __init__ is `init`."""
    for name in [m for m in sys.modules if m == "ee" or m.startswith("ee.")]:
        monkeypatch.delitem(sys.modules, name)
    package = tmp_path / "ee"
    package.mkdir()
    (package / "__init__.py").write_text(init)
    monkeypatch.syspath_prepend(str(tmp_path))


def test_without_the_overlay_only_free_routes_load(monkeypatch):
    monkeypatch.setitem(sys.modules, "ee", None)        # `import ee` → ModuleNotFoundError(name='ee')
    main = _load_main()
    assert ("GET", "/api/health") in main.ROUTES
    assert ("GET", "/api/overlay-probe") not in main.ROUTES


def test_a_present_overlay_adds_its_routes(monkeypatch, tmp_path):
    _overlay(monkeypatch, tmp_path, (
        "import types\n"
        "def _register(router):\n"
        "    router.add('GET', '/api/overlay-probe', lambda event: {})\n"
        "MODULES = [types.SimpleNamespace(register=_register)]\n"
    ))
    assert ("GET", "/api/overlay-probe") in _load_main().ROUTES


def test_an_overlay_missing_a_dependency_fails_loudly(monkeypatch, tmp_path):
    _overlay(monkeypatch, tmp_path, "import polyris_overlay_dependency_that_is_absent\nMODULES = []\n")
    with pytest.raises(ModuleNotFoundError) as caught:
        _load_main()
    assert caught.value.name == "polyris_overlay_dependency_that_is_absent"


def test_an_overlay_importing_a_removed_name_fails_loudly(monkeypatch, tmp_path):
    # The shape of a CE change the overlay has not caught up with yet.
    _overlay(monkeypatch, tmp_path, "from dal import repo_that_was_removed\nMODULES = []\n")
    with pytest.raises(ImportError, match="repo_that_was_removed"):
        _load_main()
