#!/usr/bin/env python3
"""Build the platform release archive that ``polyris-up`` installs (ADR #131).

Run by .github/workflows/release.yml on every ``v*`` tag, after ``sam build``
and the UI build. Can also be run locally to test ``polyris-up`` against an
unreleased build (``polyris-up --archive dist/polyris-platform-vX.Y.Z.zip``).

    cd sam && sam build --build-dir ../build/sam && cd ..
    (cd ui && npm ci && npm run build)
    python scripts/build_platform_release.py --version 1.2.0 \\
        --sam-build-dir build/sam --ui-dir ui/out --out dist

Archive layout (contract in ``polyris/platform.py``):

    manifest.json                 Manifest.to_dict()
    template.json                 sam/template.yaml as JSON, local paths untouched
    lambdas/<LogicalId>.zip       built code of each AWS::Serverless::Function
    sfn/<LogicalId>.json          DefinitionUri file of each AWS::Serverless::StateMachine
    ui/...                        the static UI export (ui/out)

All zips are deterministic (sorted entries, fixed timestamps), so rebuilding
an unchanged function yields the same bytes, the same digest and the same S3
key — and CloudFormation leaves that function alone.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from polyris.platform import Manifest, release_asset_name, rewrite_template  # noqa: E402

SAM_DIR = REPO_ROOT / "sam"
TEMPLATE_PATH = SAM_DIR / "template.yaml"

_FIXED_DATE = (1980, 1, 1, 0, 0, 0)

# CloudFormation short-form tags → long-form JSON keys.
_FN_TAGS = {
    "!Ref": "Ref",
    "!Condition": "Condition",
    "!Base64": "Fn::Base64",
    "!Cidr": "Fn::Cidr",
    "!FindInMap": "Fn::FindInMap",
    "!GetAZs": "Fn::GetAZs",
    "!ImportValue": "Fn::ImportValue",
    "!Join": "Fn::Join",
    "!Select": "Fn::Select",
    "!Split": "Fn::Split",
    "!Sub": "Fn::Sub",
    "!Transform": "Fn::Transform",
    "!And": "Fn::And",
    "!Equals": "Fn::Equals",
    "!If": "Fn::If",
    "!Not": "Fn::Not",
    "!Or": "Fn::Or",
}


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that understands CloudFormation's short-form intrinsic tags."""


def _construct(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)  # type: ignore[arg-type]


def _make_fn(key: str):
    def construct(loader: yaml.SafeLoader, node: yaml.Node) -> Dict[str, Any]:
        return {key: _construct(loader, node)}
    return construct


def _get_att(loader: yaml.SafeLoader, node: yaml.Node) -> Dict[str, Any]:
    value = _construct(loader, node)
    if isinstance(value, str):
        # "Resource.Attr.Maybe.Dotted" → ["Resource", "Attr.Maybe.Dotted"]
        resource, _, attr = value.partition(".")
        value = [resource, attr]
    return {"Fn::GetAtt": value}


for _tag, _key in _FN_TAGS.items():
    _CfnLoader.add_constructor(_tag, _make_fn(_key))
_CfnLoader.add_constructor("!GetAtt", _get_att)


def load_cfn_yaml(text: str) -> Dict[str, Any]:
    """Parse a CloudFormation YAML template into its long-form JSON structure."""
    data = yaml.load(text, Loader=_CfnLoader)  # noqa: S506 — custom SafeLoader subclass
    if not isinstance(data, dict):
        raise ValueError("Template is not a mapping")
    return data


def _write_deterministic_zip(src_dir: Path, dest: Path) -> None:
    """Zip a directory tree with stable ordering, timestamps and permissions."""
    files = sorted(p for p in src_dir.rglob("*") if p.is_file())
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(src_dir).as_posix(), date_time=_FIXED_DATE)
            mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
            info.external_attr = (0o100000 | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())


def build_archive(*, version: str, sam_build_dir: Path, ui_dir: Path, out_dir: Path) -> Path:
    """Assemble the release archive and return its path."""
    template = load_cfn_yaml(TEMPLATE_PATH.read_text())
    resources = template.get("Resources") or {}
    if not (ui_dir / "index.html").is_file():
        raise FileNotFoundError(f"UI build not found: {ui_dir}/index.html (run `npm run build` in ui/)")

    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        (stage / "lambdas").mkdir()
        (stage / "sfn").mkdir()

        functions: Dict[str, str] = {}
        state_machines: Dict[str, str] = {}
        for logical_id, res in sorted(resources.items()):
            props = res.get("Properties") or {}
            if res.get("Type") == "AWS::Serverless::Function" and isinstance(props.get("CodeUri"), str):
                built = sam_build_dir / logical_id
                if not built.is_dir():
                    raise FileNotFoundError(f"sam build output missing for {logical_id}: {built}")
                rel = f"lambdas/{logical_id}.zip"
                _write_deterministic_zip(built, stage / rel)
                functions[logical_id] = rel
            elif res.get("Type") == "AWS::Serverless::StateMachine" and isinstance(props.get("DefinitionUri"), str):
                rel = f"sfn/{logical_id}.json"
                shutil.copyfile(SAM_DIR / props["DefinitionUri"], stage / rel)
                state_machines[logical_id] = rel

        (stage / "template.json").write_text(json.dumps(template, indent=1, sort_keys=True))
        shutil.copytree(ui_dir, stage / "ui")
        manifest = Manifest(
            version=version, template="template.json", ui="ui",
            functions=functions, state_machines=state_machines,
        )
        (stage / "manifest.json").write_text(json.dumps(manifest.to_dict(), indent=2))

        out_dir.mkdir(parents=True, exist_ok=True)
        archive = out_dir / release_asset_name(version)
        _write_deterministic_zip(stage, archive)
    return archive


def write_deployable_template(archive: Path, dest: Path) -> None:
    """Write the template exactly as polyris-up would deploy it, with a placeholder bucket.

    CI runs cfn-lint on the result, so a release whose rewritten template is
    invalid fails on the pull request rather than at install time.
    """
    with zipfile.ZipFile(archive) as zf:
        manifest = Manifest.from_dict(json.loads(zf.read("manifest.json")))
        template = json.loads(zf.read(manifest.template))
    bucket = "polyris-artifacts-123456789012-us-east-1"
    rewritten = rewrite_template(
        template,
        bucket=bucket,
        function_keys={lid: f"v{manifest.version}/lambdas/0/{lid}.zip" for lid in manifest.functions},
        state_machine_keys={lid: f"v{manifest.version}/sfn/0/{lid}.json" for lid in manifest.state_machines},
        version=manifest.version,
    )
    dest.write_text(json.dumps(rewritten, indent=1))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", required=True, help="Release version without the leading v (e.g. 1.2.0)")
    parser.add_argument("--sam-build-dir", required=True, type=Path, help="Output of `sam build --build-dir`")
    parser.add_argument("--ui-dir", required=True, type=Path, help="Static UI export (ui/out)")
    parser.add_argument("--out", required=True, type=Path, help="Directory to write the archive into")
    parser.add_argument("--write-deployable-template", type=Path, metavar="PATH",
                        help="Also write the template as polyris-up would deploy it (for cfn-lint in CI)")
    args = parser.parse_args(argv)

    version = args.version.removeprefix("v")
    archive = build_archive(version=version, sam_build_dir=args.sam_build_dir, ui_dir=args.ui_dir, out_dir=args.out)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (archive.parent / f"{archive.name}.sha256").write_text(f"{digest}  {archive.name}\n")
    print(f"{archive} ({archive.stat().st_size // 1024} KiB) sha256={digest}")
    if args.write_deployable_template:
        write_deployable_template(archive, args.write_deployable_template)
        print(f"deployable template → {args.write_deployable_template}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
