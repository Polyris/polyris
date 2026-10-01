"""Tests for polyris-build-lambda (stamp XCom helpers into Lambda dirs)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from polyris.build_lambda import stamp, main


class TestStamp:
    def test_creates_package_directory(self, tmp_path: Path):
        dest = tmp_path / "lambda_dir"
        dest.mkdir()
        pkg_dir = stamp(dest)
        assert pkg_dir.is_dir()
        assert pkg_dir == dest / "polyris"

    def test_creates_xcom_py(self, tmp_path: Path):
        dest = tmp_path / "lambda_dir"
        dest.mkdir()
        pkg_dir = stamp(dest)
        assert (pkg_dir / "xcom.py").is_file()

    def test_creates_init_py(self, tmp_path: Path):
        dest = tmp_path / "lambda_dir"
        dest.mkdir()
        pkg_dir = stamp(dest)
        assert (pkg_dir / "__init__.py").is_file()

    def test_xcom_py_contains_runtime_functions(self, tmp_path: Path):
        dest = tmp_path / "lambda_dir"
        dest.mkdir()
        pkg_dir = stamp(dest)
        text = (pkg_dir / "xcom.py").read_text()
        assert "def get(" in text
        assert "def push(" in text
        assert "def pull(" in text

    def test_init_py_imports_xcom_module(self, tmp_path: Path):
        dest = tmp_path / "lambda_dir"
        dest.mkdir()
        pkg_dir = stamp(dest)
        text = (pkg_dir / "__init__.py").read_text()
        assert "from . import xcom" in text

    def test_missing_dest_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="not found"):
            stamp(tmp_path / "does_not_exist")

    def test_dest_is_file_raises(self, tmp_path: Path):
        f = tmp_path / "not_a_dir"
        f.touch()
        with pytest.raises(NotADirectoryError):
            stamp(f)

    def test_existing_polyris_dir_raises_without_force(self, tmp_path: Path):
        dest = tmp_path / "lambda_dir"
        dest.mkdir()
        stamp(dest)
        with pytest.raises(FileExistsError, match="already exists"):
            stamp(dest)

    def test_force_overwrites_existing(self, tmp_path: Path):
        dest = tmp_path / "lambda_dir"
        dest.mkdir()
        stamp(dest)
        pkg_dir = stamp(dest, force=True)
        assert (pkg_dir / "xcom.py").is_file()

    def test_generated_xcom_is_importable_standalone(self, tmp_path: Path):
        """xcom.py has no relative imports — it loads without the polyris package."""
        dest = tmp_path / "lambda_dir"
        dest.mkdir()
        pkg_dir = stamp(dest)
        spec = importlib.util.spec_from_file_location(
            "_test_generated_xcom", pkg_dir / "xcom.py"
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[attr-defined]
        assert callable(mod.get)
        assert callable(mod.push)
        assert callable(mod.pull)


class TestCLI:
    def test_success_returns_0(self, tmp_path: Path):
        dest = tmp_path / "lambda_dir"
        dest.mkdir()
        assert main([str(dest)]) == 0

    def test_missing_dir_returns_1(self, tmp_path: Path):
        assert main([str(tmp_path / "does_not_exist")]) == 1

    def test_dest_is_file_returns_1(self, tmp_path: Path):
        f = tmp_path / "not_a_dir"
        f.touch()
        assert main([str(f)]) == 1

    def test_existing_without_force_returns_1(self, tmp_path: Path):
        dest = tmp_path / "lambda_dir"
        dest.mkdir()
        main([str(dest)])
        assert main([str(dest)]) == 1

    def test_force_flag_returns_0_on_existing(self, tmp_path: Path):
        dest = tmp_path / "lambda_dir"
        dest.mkdir()
        main([str(dest)])
        assert main([str(dest), "--force"]) == 0

    def test_prints_version_and_path(self, tmp_path: Path, capsys):
        dest = tmp_path / "lambda_dir"
        dest.mkdir()
        main([str(dest)])
        out = capsys.readouterr().out
        assert "polyris" in out
        assert "from polyris import xcom" in out
