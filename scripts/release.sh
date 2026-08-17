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

# ── Gates ──────────────────────────────────────────────────────────────────────
# Run gates first — a failing gate must never leave version files modified.

if [[ "$MODE" == "lib" ]]; then
  echo "→ running library gates (sdk tests + mypy + ruff)"
  make test-sdk
  mypy polyris/ --ignore-missing-imports
  ruff check polyris/
else
  echo "→ running full gates (make check)"
  make check
fi

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

echo "→ bumping sam/lambdas/console_api/requirements.txt"
sed -i "s|polyris @ git+https://github.com/Polyris/polyris@v[0-9]*\.[0-9]*\.[0-9]*|polyris @ git+https://github.com/Polyris/polyris@$VERSION|" \
  sam/lambdas/console_api/requirements.txt

make check-versions

# ── Commit + tag ───────────────────────────────────────────────────────────────

git add pyproject.toml polyris/__init__.py ui/package.json \
  sam/lambdas/console_api/requirements.txt

if git diff --cached --quiet; then
  echo "→ versions already at $BARE in git — skipping bump commit"
else
  git commit -m "chore: release $VERSION"
fi

git tag "$VERSION"

echo ""
echo "✅ commit + tag $VERSION ready"
echo ""
read -r -p "Push to origin? [y/N] " confirm
if [[ "$confirm" =~ ^[Yy]$ ]]; then
  git push origin main "$VERSION"
  echo "✅ $VERSION pushed"
else
  git tag -d "$VERSION"
  git reset --soft HEAD~1
  echo "ℹ️  rolled back — fix anything and re-run: ./scripts/release.sh $MODE $VERSION"
  exit 0
fi

# ── GitHub Release ─────────────────────────────────────────────────────────────
# Uses `gh release create --generate-notes`, which pulls PR titles + commit
# subjects between the previous tag and this one and formats them itself
# (contributor list + "Full Changelog" link included). No AI — straight from
# git history, so the notes never claim work that wasn't done. Edit after
# with `gh release edit $VERSION --notes-file <file>` if you want a custom
# summary on top.

if ! command -v gh &>/dev/null; then
  echo "ℹ️  gh not found — skipping GitHub Release"
  echo "   Create manually: gh release create $VERSION --title $VERSION --generate-notes"
  exit 0
fi

read -r -p "Create GitHub Release with auto-generated notes? [y/N] " confirm
if [[ "$confirm" =~ ^[Yy]$ ]]; then
  gh release create "$VERSION" --title "$VERSION" --generate-notes
  echo "✅ GitHub Release $VERSION created"
else
  echo "ℹ️  skipped — create manually: gh release create $VERSION --title $VERSION --generate-notes"
fi
