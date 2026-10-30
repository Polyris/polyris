#!/usr/bin/env bash
# ── verify-changed.sh ────────────────────────────────────────────────────────
# Runs the gates that the current working-tree changes actually require.
#
# Why this exists: the full gate set takes ~4 minutes, most of it the UI build.
# Running everything after every edit is too slow to be honest about, so it gets
# skipped — and "I ran the tests" quietly becomes "I ran some tests". This picks
# the gates by what changed, so the fast path stays fast and nothing relevant is
# ever skipped by judgement.
#
# Wired as a Claude Code Stop hook (.claude/settings.json), so a session cannot
# end on red without it being visible. Also runnable by hand: bash
# scripts/verify-changed.sh [--full]
#
# Exit 0 = everything the change touched is green.
set -uo pipefail
cd "$(dirname "$0")/.."

FULL=0
[ "${1:-}" = "--full" ] && FULL=1

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  CHANGED="$(git status --porcelain | sed 's/ -> /\n/g' | awk '{print $NF}')"
else
  # Not a checkout (unpacked archive): cannot scope, so verify everything.
  CHANGED=""
  FULL=1
fi

touched() { [ "$FULL" = 1 ] && return 0; printf '%s\n' "$CHANGED" | grep -qE "$1"; }

FAILED=()
run() {  # run <label> <command...>
  local label="$1"; shift
  printf '  %-34s' "$label"
  if out=$("$@" 2>&1); then
    echo "OK"
  else
    echo "FAIL"
    FAILED+=("$label")
    printf '%s\n' "$out" | tail -25 | sed 's/^/      /'
  fi
}

echo "── verify-changed ──────────────────────────────────────────"
[ "$FULL" = 1 ] && echo "  (full run)" || echo "  changed: $(printf '%s\n' "$CHANGED" | wc -l) path(s)"

# ── Python core / SDK ───────────────────────────────────────────────────────
if touched '^(polyris/|tests/|pyproject\.toml)'; then
  run "ruff"                    ruff check .
  run "mypy (py.typed contract)" python3 -m mypy polyris/ --ignore-missing-imports
  run "pytest + coverage floor" make test-cov
fi

# ── Lambdas / SAM ───────────────────────────────────────────────────────────
if touched '^(sam/|polyris/constants\.py|polyris/codegen/)'; then
  run "cfn-lint"                cfn-lint sam/template.yaml
  run "constants + loggers"     make sync-constants
  run "enum codegen drift"      make check-generate-enums
  run "dateVars codegen drift"  make check-generate-variables
  run "SFN status literals"     make check-sfn-templates
  run "backfill parity"         make check-backfill-parity
  for d in evaluate_deps notify notify_asset_subscribers check_assets query_subscriptions bucket_cleanup; do
    run "lambda: $d" bash -c "cd sam/lambdas/$d && PYTHONPATH=. python3 -m pytest -q -p no:cacheprovider"
  done
  run "console_api" bash -c "cd sam/lambdas/console_api && PYTHONPATH=. python3 -m pytest tests/ -q -p no:cacheprovider"
fi

# ── UI ──────────────────────────────────────────────────────────────────────
if touched '^ui/'; then
  if [ -d ui/node_modules ]; then
    run "ui: typecheck"  bash -c "cd ui && npm run typecheck"
    run "ui: eslint"     bash -c "cd ui && npm run lint"
    run "ui: vitest"     bash -c "cd ui && npx vitest run"
    run "ui: build"      bash -c "cd ui && npm run build"
    run "ui: oss guard"  bash -c "cd ui && bash scripts/check-oss-build.sh"
  else
    echo "  ui gates SKIPPED — run 'cd ui && npm ci' first" >&2
    FAILED+=("ui gates could not run (no node_modules)")
  fi
fi

# ── Always ──────────────────────────────────────────────────────────────────
run "open-core guard"   bash scripts/check-no-paid.sh
run "version consistency" make check-versions

# ── Soft warnings (non-blocking) ────────────────────────────────────────────
WARNINGS=()

# CHANGELOG: if there are commits ahead of main but CHANGELOG.md hasn't changed
base_ref=$(git rev-parse --verify origin/main 2>/dev/null || git rev-parse --verify main 2>/dev/null || echo "")
if [ -n "$base_ref" ]; then
  commits_ahead=$(git rev-list "${base_ref}..HEAD" --count 2>/dev/null || echo 0)
  if [ "$commits_ahead" -gt 0 ]; then
    code_changed=$(git diff "${base_ref}..HEAD" --name-only 2>/dev/null | grep -cE '\.(py|ts|tsx|json|yaml)$' || echo 0)
    if [ "$code_changed" -gt 0 ]; then
      if ! git diff "${base_ref}..HEAD" --name-only 2>/dev/null | grep -q "^CHANGELOG\.md$"; then
        WARNINGS+=("CHANGELOG.md not updated ($commits_ahead commit(s) ahead of origin/main, $code_changed code file(s) changed)")
      fi
    fi
  fi
fi

# adr-index.md: declared count must match actual row count in the table
if [ -f docs/reference/adr-index.md ]; then
  declared=$(grep -oP '_\K[0-9]+(?= rows indexed)' docs/reference/adr-index.md 2>/dev/null || echo "")
  actual=$(grep -cE "^\| [0-9]+ \| " docs/reference/adr-index.md 2>/dev/null || echo 0)
  if [ -n "$declared" ] && [ "$declared" != "$actual" ]; then
    WARNINGS+=("adr-index.md declares $declared ADRs but table has $actual rows (Principle #29)")
  fi
fi

if [ ${#WARNINGS[@]} -gt 0 ]; then
  echo ""
  echo "⚠️  ${#WARNINGS[@]} warning(s):"
  printf '   - %s\n' "${WARNINGS[@]}"
fi

echo "────────────────────────────────────────────────────────────"
if [ ${#FAILED[@]} -eq 0 ]; then
  echo "✅ green"
  if [ -z "$(git status --porcelain)" ]; then
    echo ""
    echo "── before you ship ─────────────────────────────────────────"
    echo "  □ /break in a NEW session (independent review)"
    echo "  □ /pr-description"
    echo "  □ CHANGELOG updated"
    echo "  □ docs updated (ADR / CLAUDE.md) if behavior changed"
    echo "────────────────────────────────────────────────────────────"
  fi
  exit 0
fi
echo "❌ ${#FAILED[@]} gate(s) failed:"
printf '   - %s\n' "${FAILED[@]}"
echo
echo "Do not report this work as done. Fix, then re-run:"
echo "  bash scripts/verify-changed.sh"
exit 1
