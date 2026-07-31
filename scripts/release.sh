#!/usr/bin/env bash
# scripts/release.sh — cut a library or full-stack release
#
# Usage:
#   ./scripts/release.sh lib  v0.1.0   # SDK-only gates (fast)
#   ./scripts/release.sh full v0.1.0   # Full gates: make check (UI + CFN + all tests)
#
# Both modes bump all three version files (pyproject.toml, polyris/__init__.py,
# ui/package.json) so check-versions stays green. The difference is gates only.
set -euo pipefail

MODE="${1:?usage: release.sh lib|full vX.Y.Z}"
VERSION="${2:?usage: release.sh lib|full vX.Y.Z}"

if [[ "$MODE" != "lib" && "$MODE" != "full" ]]; then
  echo "❌ unknown mode: $MODE (expected lib or full)" >&2; exit 1
fi

if [[ ! "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "❌ version must be vX.Y.Z (got: $VERSION)" >&2; exit 1
fi

BARE="${VERSION#v}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# ── Preflight ──────────────────────────────────────────────────────────────────

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$BRANCH" != "main" ]]; then
  echo "❌ must be on main (currently on $BRANCH)" >&2; exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "❌ working tree is dirty — commit or stash changes first" >&2; exit 1
fi

if git rev-parse "$VERSION" >/dev/null 2>&1; then
  echo "❌ tag $VERSION already exists" >&2; exit 1
fi

echo "→ mode: $MODE   version: $VERSION"

# ── Bump versions ──────────────────────────────────────────────────────────────

echo "→ bumping pyproject.toml"
sed -i "s/^version = \".*\"/version = \"$BARE\"/" pyproject.toml

echo "→ bumping polyris/__init__.py"
sed -i "s/__version__ = \".*\"/__version__ = \"$BARE\"/" polyris/__init__.py

echo "→ bumping ui/package.json"
node -e "
  const fs = require('fs');
  const p = JSON.parse(fs.readFileSync('ui/package.json', 'utf8'));
  p.version = '$BARE';
  fs.writeFileSync('ui/package.json', JSON.stringify(p, null, 2) + '\n');
"

make check-versions

# ── Gates ──────────────────────────────────────────────────────────────────────

if [[ "$MODE" == "lib" ]]; then
  echo "→ running library gates (sdk tests + mypy + ruff)"
  make test-sdk
  mypy polyris/ --ignore-missing-imports
  ruff check polyris/
else
  echo "→ running full gates (make check)"
  make check
fi

# ── Commit + tag ───────────────────────────────────────────────────────────────

git add pyproject.toml polyris/__init__.py ui/package.json
git commit -m "chore: release $VERSION"
git tag "$VERSION"

echo ""
echo "✅ commit + tag $VERSION ready"
echo ""
read -r -p "Push to origin? [y/N] " confirm
if [[ "$confirm" =~ ^[Yy]$ ]]; then
  git push origin main "$VERSION"
  echo "✅ $VERSION pushed"
else
  echo "ℹ️  not pushed — run when ready: git push origin main $VERSION"
fi
