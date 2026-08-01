"""
CLAUDE.md claim guard.

CLAUDE.md is the contract every agent session reads and trusts. When one of its
claims stops being true, nothing notices — the document keeps asserting it and
the next session builds on a false premise. That is not hypothetical: at v0.93.0
four claims were false and had been for some time ("DAL 100%", "no
unittest.mock", the coverage-omit justification, and an ADR #99 example), and
they were found by accident rather than by a gate.

This file mechanises the claims that *can* be mechanised. Each test names the
claim it defends. If a claim becomes false, this goes red — the document cannot
drift further than one `make check`.

Claims requiring judgement ("follow existing patterns", "don't build for
hypothetical use cases") are deliberately absent; they belong to review, not to
a test. If you add a checkable claim to CLAUDE.md, add its assertion here.
"""

import ast
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLAUDE_MD = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")


def _py_files(*roots, exclude_tests=False):
    out = []
    for r in roots:
        for p in (ROOT / r).rglob("*.py"):
            s = str(p)
            if "__pycache__" in s or ".aws-sam" in s:
                continue
            if exclude_tests and ("/tests/" in s or "/test_" in s or p.name.startswith("test_")):
                continue
            out.append(p)
    return out


def _ts_files(exclude_tests=True):
    out = []
    for ext in ("*.ts", "*.tsx"):
        for p in (ROOT / "ui/src").rglob(ext):
            s = str(p)
            if "node_modules" in s:
                continue
            if exclude_tests and (".test." in p.name or "/test/" in s):
                continue
            out.append(p)
    return out


def _claims(*fragments):
    """Fail loudly if the claim text itself was removed or reworded — a silently
    deleted claim is drift too."""
    for f in fragments:
        assert f in CLAUDE_MD, (
            f"CLAUDE.md no longer contains the claim {f!r}. If the claim was "
            f"intentionally dropped, delete the guard for it in the same change."
        )


# ── "No wildcard imports (0 across all .py files)" ───────────────────────────

def test_no_wildcard_imports():
    _claims("No wildcard imports")
    bad = []
    for p in _py_files("polyris", "sam", "tests"):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
                bad.append(f"{p.relative_to(ROOT)}:{node.lineno}")
    assert not bad, "CLAUDE.md claims zero wildcard imports:\n  " + "\n  ".join(bad)


# ── "DAL repositories for all DynamoDB access in console_api" ────────────────

def test_console_api_never_takes_a_raw_table_handle():
    _claims("DAL repositories for all DynamoDB access in `console_api`")
    api = ROOT / "sam/lambdas/console_api"
    allowed = {"config.py"}  # the DAL's own table factory
    bad = []
    for p in api.rglob("*.py"):
        s = str(p)
        if "__pycache__" in s or "/dal/" in s or "/tests/" in s or p.name.startswith("test_"):
            continue
        if p.name in allowed:
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "Table"):
                bad.append(f"{p.relative_to(ROOT)}:{node.lineno}")
    assert not bad, (
        "Route/helper code reached past the DAL for a raw table handle. Add a "
        "repo method in console_api/dal/ instead:\n  " + "\n  ".join(bad)
    )


# ── "pytest-mock (mocker) everywhere — no unittest.mock import at all" ───────

def test_no_unittest_mock_anywhere():
    _claims("no `unittest.mock` import at all")
    bad = []
    for p in _py_files("tests", "sam", "polyris"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("unittest.mock"):
                bad.append(f"{p.relative_to(ROOT)}:{node.lineno}")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("unittest.mock"):
                        bad.append(f"{p.relative_to(ROOT)}:{node.lineno}")
    assert not bad, (
        "ADR #26: use the pytest-mock `mocker` fixture (mocker.patch, "
        "mocker.MagicMock):\n  " + "\n  ".join(bad)
    )


# ── "No .module.css files (0)" ───────────────────────────────────────────────

def test_no_css_modules():
    _claims("No `.module.css` files")
    bad = [str(p.relative_to(ROOT)) for p in (ROOT / "ui/src").rglob("*.module.css")]
    assert not bad, "CLAUDE.md: .module.css files are dead here:\n  " + "\n  ".join(bad)


# ── "Components never import api directly (100%)" ────────────────────────────

def test_components_never_import_api_directly():
    _claims("Components never import `api` directly")
    bad = []
    pattern = re.compile(r"""import\s*\{[^}]*\bapi\b[^}]*\}\s*from\s*['"][^'"]*utils(/api)?['"]""")
    for p in (ROOT / "ui/src/components").rglob("*.tsx"):
        if ".test." in p.name:
            continue
        if pattern.search(p.read_text(encoding="utf-8")):
            bad.append(str(p.relative_to(ROOT)))
    assert not bad, (
        "Components must reach the API through hooks/queries, not `api`:\n  "
        + "\n  ".join(bad)
    )


# ── "shadcn primitives only, no other UI libraries" ──────────────────────────

def test_no_competing_ui_libraries():
    _claims("shadcn primitives only, no other UI libraries")
    banned = ("@mui/", "@chakra-ui/", "@mantine/", "antd", "react-bootstrap")
    pkg = (ROOT / "ui/package.json").read_text(encoding="utf-8")
    found = [b for b in banned if f'"{b}' in pkg]
    assert not found, f"CLAUDE.md forbids other component libraries; found: {found}"


# ── "TypeScript strict, no `any` in production code" ─────────────────────────

def test_typescript_strict_is_on():
    _claims("TypeScript strict")
    tsconfig = (ROOT / "ui/tsconfig.json").read_text(encoding="utf-8")
    assert re.search(r'"strict"\s*:\s*true', tsconfig), (
        "CLAUDE.md claims TypeScript strict mode; tsconfig.json does not set it."
    )


# ── "Stateless Lambdas, no warm-state caching" is judgement; skipped. ────────
# ── "Type hints on public Python functions (91%)" — a floor, not an absolute ──

def test_public_type_hint_floor_holds():
    _claims("Type hints on public Python functions")
    claimed = int(re.search(r"Type hints on public Python functions \((\d+)%\)", CLAUDE_MD).group(1))
    total = hinted = 0
    for p in _py_files("polyris"):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                total += 1
                if node.returns or any(a.annotation for a in node.args.args):
                    hinted += 1
    actual = hinted * 100 // total
    assert actual >= claimed, (
        f"CLAUDE.md claims {claimed}% type-hinted public functions; measured {actual}%. "
        f"Either raise coverage or update the claim in the same change."
    )


# ── Guards must fail closed, not report success when they cannot run ─────────

@pytest.mark.parametrize("script", ["scripts/check-no-paid.sh"])
def test_leak_guard_fails_outside_a_git_worktree(script, tmp_path):
    """Both leak guards once printed a git error and still exited 0. A guard that
    passes when it cannot run is worse than no guard, so prove it fails."""
    staged = tmp_path / "repo"
    staged.mkdir()
    (staged / "scripts").mkdir()
    src = ROOT / script
    dst = staged / script
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    dst.chmod(0o755)
    r = subprocess.run(["bash", str(dst)], capture_output=True, text=True)
    assert r.returncode != 0, (
        f"{script} reported success outside a git work tree. It must fail closed."
    )


def test_every_gate_in_make_check_exists():
    """`make check` is the contract for "I ran the gates". A target that silently
    does not exist would make it a no-op."""
    mk = (ROOT / "Makefile").read_text(encoding="utf-8")
    body = re.search(r"^check:(.*?)\n\t", mk, re.S | re.M).group(1)
    targets = [t for t in body.replace("\\", " ").split() if t]
    for t in targets:
        assert re.search(rf"^{re.escape(t)}:", mk, re.M), (
            f"`make check` depends on '{t}', which is not defined in the Makefile."
        )
