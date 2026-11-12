"""Tests for scripts/build_platform_release.py — the CI side of the release contract (ADR #131)."""
from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest

from polyris.platform import Manifest, rewrite_template

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("build_platform_release",
                                               REPO_ROOT / "scripts" / "build_platform_release.py")
assert _spec and _spec.loader
build = importlib.util.module_from_spec(_spec)
sys.modules["build_platform_release"] = build
_spec.loader.exec_module(build)


def test_short_form_tags_become_long_form():
    tpl = build.load_cfn_yaml(
        "A: !Ref X\n"
        "B: !GetAtt Res.Arn\n"
        "C: !GetAtt Res.Nested.Attr\n"
        "D: !Sub ['${x}', {x: !Ref Y}]\n"
        "E: !If [Cond, !Ref AWS::NoValue, 1]\n"
        "F: !Condition Other\n"
        "G: !GetAtt [Res, Arn]\n"
    )
    assert tpl == {
        "A": {"Ref": "X"},
        "B": {"Fn::GetAtt": ["Res", "Arn"]},
        "C": {"Fn::GetAtt": ["Res", "Nested.Attr"]},
        "D": {"Fn::Sub": ["${x}", {"x": {"Ref": "Y"}}]},
        "E": {"Fn::If": ["Cond", {"Ref": "AWS::NoValue"}, 1]},
        "F": {"Condition": "Other"},
        "G": {"Fn::GetAtt": ["Res", "Arn"]},
    }


def test_rejects_non_mapping_template():
    with pytest.raises(ValueError, match="not a mapping"):
        build.load_cfn_yaml("- just\n- a list\n")


def test_real_template_converts_without_leftover_tags():
    text = json.dumps(build.load_cfn_yaml(build.TEMPLATE_PATH.read_text()))
    assert '"!' not in text


def _fake_sam_build(tmp_path: Path) -> Path:
    tpl = build.load_cfn_yaml(build.TEMPLATE_PATH.read_text())
    out = tmp_path / "sam-build"
    for lid, res in tpl["Resources"].items():
        if res["Type"] == "AWS::Serverless::Function":
            (out / lid).mkdir(parents=True)
            (out / lid / "index.py").write_text(f"# {lid}\n")
    return out


def _ui(tmp_path: Path) -> Path:
    ui = tmp_path / "ui-out"
    (ui / "_next").mkdir(parents=True)
    (ui / "index.html").write_text("<html>")
    (ui / "_next" / "app.js").write_text("js")
    return ui


def test_build_archive_matches_template_and_is_deterministic(tmp_path):
    sam_build, ui = _fake_sam_build(tmp_path), _ui(tmp_path)

    first = build.build_archive(version="9.9.9", sam_build_dir=sam_build, ui_dir=ui, out_dir=tmp_path / "a")
    second = build.build_archive(version="9.9.9", sam_build_dir=sam_build, ui_dir=ui, out_dir=tmp_path / "b")

    assert first.name == "polyris-platform-v9.9.9.zip"
    assert first.read_bytes() == second.read_bytes(), "same inputs must give identical bytes"
    with zipfile.ZipFile(first) as zf:
        manifest = Manifest.from_dict(json.loads(zf.read("manifest.json")))
        template = json.loads(zf.read("template.json"))
        names = set(zf.namelist())
    assert manifest.version == "9.9.9"
    assert set(manifest.functions.values()) | set(manifest.state_machines.values()) | {"ui/index.html"} <= names
    # Every local path in the shipped template is covered by the manifest.
    rewrite_template(template, bucket="bkt", function_keys=manifest.functions,
                     state_machine_keys=manifest.state_machines, version="9.9.9")


def test_build_archive_requires_ui_build(tmp_path):
    with pytest.raises(FileNotFoundError, match="UI build not found"):
        build.build_archive(version="1", sam_build_dir=tmp_path, ui_dir=tmp_path / "nope", out_dir=tmp_path)


def test_build_archive_requires_every_function_built(tmp_path):
    with pytest.raises(FileNotFoundError, match="sam build output missing"):
        build.build_archive(version="1", sam_build_dir=tmp_path / "empty", ui_dir=_ui(tmp_path), out_dir=tmp_path)


def test_main_writes_checksum(tmp_path, capsys):
    rc = build.main(["--version", "v9.9.9", "--sam-build-dir", str(_fake_sam_build(tmp_path)),
                     "--ui-dir", str(_ui(tmp_path)), "--out", str(tmp_path / "dist")])
    assert rc == 0
    sha = (tmp_path / "dist" / "polyris-platform-v9.9.9.zip.sha256").read_text().split()
    assert sha[1] == "polyris-platform-v9.9.9.zip" and len(sha[0]) == 64
    assert "sha256=" in capsys.readouterr().out


def test_deployable_template_points_every_local_path_at_s3(tmp_path):
    archive = build.build_archive(version="9.9.9", sam_build_dir=_fake_sam_build(tmp_path), ui_dir=_ui(tmp_path),
                                  out_dir=tmp_path / "dist")
    dest = tmp_path / "deployable.json"
    build.write_deployable_template(archive, dest)

    tpl = json.loads(dest.read_text())
    assert tpl["Description"] == "Polyris platform v9.9.9"
    for res in tpl["Resources"].values():
        props = res.get("Properties") or {}
        for key in ("CodeUri", "DefinitionUri"):
            if key in props:
                assert isinstance(props[key], dict) and props[key]["Bucket"].startswith("polyris-artifacts-")


def test_main_writes_deployable_template_on_request(tmp_path):
    dest = tmp_path / "t.json"
    assert build.main(["--version", "1", "--sam-build-dir", str(_fake_sam_build(tmp_path)),
                       "--ui-dir", str(_ui(tmp_path)), "--out", str(tmp_path / "d"),
                       "--write-deployable-template", str(dest)]) == 0
    assert dest.is_file()
