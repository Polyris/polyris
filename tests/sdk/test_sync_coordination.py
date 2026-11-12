"""The generated `coordination_core` copies (ADR #130 §10)."""

import subprocess
import sys
import textwrap

import pytest

from polyris.codegen import sync_coordination as sc
from polyris.coordination import statuses


def test_committed_copies_are_in_sync():
    assert sc.sync(sc.TARGET_DIRS, write=False) == [], (
        "run: python -m polyris.codegen.sync_coordination"
    )


def test_copies_never_import_outside_the_package():
    for name, text in sc.render().items():
        assert not sc._FORBIDDEN.search(text), name
    assert "from .partitions import" in sc.render()["partition.py"]


def test_literal_statuses_match_the_source_of_truth():
    namespace: dict = {}
    exec(sc.render()["statuses.py"], namespace)
    for name in ("RECONCILABLE", "TERMINAL", "SUCCESS_ONLY", "FAILURE", "SKIPPED"):
        assert namespace[name] == getattr(statuses, name), name


def test_a_module_reaching_outside_the_package_is_refused(tmp_path):
    (tmp_path / "leaky.py").write_text("from polyris.constants import TaskStatus\n")
    with pytest.raises(ValueError, match="leaky.py"):
        sc.render(source_dir=tmp_path)


def test_sync_writes_then_reports_nothing_and_removes_stale_files(tmp_path):
    target = tmp_path / sc.PACKAGE
    assert sc.sync([target], write=False)          # nothing there yet
    assert not target.exists()                      # check mode never writes
    written = sc.sync([target], write=True)
    assert {p.name for p in written} == set(sc.render())
    assert sc.sync([target], write=False) == []

    (target / "removed_module.py").write_text("x = 1\n")
    assert [p.name for p in sc.sync([target], write=False)] == ["removed_module.py"]
    sc.sync([target], write=True)
    assert not (target / "removed_module.py").exists()


def test_generated_package_runs_without_polyris(tmp_path):
    sc.sync([tmp_path / sc.PACKAGE], write=True)
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(tmp_path)!r})
        sys.modules["polyris"] = None  # any `import polyris` now fails
        from coordination_core import decide
        task = decide.TaskView("waiting", False, "all_success", ("up",), False)
        print(decide.decide(task, dep_statuses=["success"]).verdict)
    """)
    out = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "ready"


class TestMain:
    def test_check_in_sync(self, monkeypatch, tmp_path, capsys):
        sc.sync([tmp_path], write=True)
        monkeypatch.setattr(sc, "TARGET_DIRS", [tmp_path])
        assert sc.main(["--check"]) == 0
        assert "in sync" in capsys.readouterr().out

    def test_check_drift(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(sc, "TARGET_DIRS", [tmp_path])
        assert sc.main(["--check"]) == 1
        assert "out of sync" in capsys.readouterr().err

    def test_write_then_nothing_to_write(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(sc, "TARGET_DIRS", [tmp_path])
        assert sc.main([]) == 0
        assert "Wrote" in capsys.readouterr().out
        assert sc.main([]) == 0
        assert "Already in sync" in capsys.readouterr().out

    def test_paths_inside_the_repo_are_shown_relative(self):
        assert sc._display(sc.TARGET_DIRS[0]) == "sam/lambdas/coordination/coordination_core"
