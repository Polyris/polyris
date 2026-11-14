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
# Wired as a Claude Code Stop hook (.claude/settings.json) with --hook, so a
# session cannot end on red without it being visible. Also runnable by hand:
# bash scripts/verify-changed.sh [--full]
#
# --hook is the light mode every session's Stop runs. It does nothing when the
# checkout is unchanged since the last green run (from any session or mode),
# and it leaves out the two slowest gates, the coverage floor and the UI
# build; a run by hand, --full and CI keep them. Every mode waits for a run
# already in progress on this checkout instead of starting a second one.
#
# Exit 0 = everything the change touched is green.
set -uo pipefail
cd "$(dirname "$0")/.."

FULL=0
HOOK=0
case "${1:-}" in
  --full) FULL=1 ;;
  --hook) HOOK=1 ;;
esac

# What the checkout looks like: HEAD, the status, every tracked change and
# every untracked file. Equal fingerprints mean nothing a gate reads changed.
fingerprint() {
  {
    git rev-parse HEAD 2>/dev/null
    git status --porcelain
    git diff HEAD --binary
    git ls-files --others --exclude-standard -z | while IFS= read -r -d '' f; do
      printf '%s %s\n' "$f" "$(git hash-object -- "$f" 2>/dev/null)"
    done
  } | git hash-object --stdin
}

PRINT=""
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  STATE_DIR="$(git rev-parse --git-dir)"
  # One run at a time per checkout: a second session's Stop waits, then
  # usually finds the first run's green mark below.
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$STATE_DIR/verify-changed.lock"
    if ! flock -n 9; then
      echo "  waiting for the verify-changed run already going on this checkout…"
      if ! flock -w 540 9; then
        echo "  that run is still going; it reports to its own session. Skipped."
        exit 0
      fi
    fi
  fi
  PRINT="$(fingerprint)"
  if [ "$HOOK" = 1 ] && [ "$(cat "$STATE_DIR/verify-changed.green" 2>/dev/null)" = "$PRINT" ]; then
    echo "── verify-changed: unchanged since the last green run ✅"
    exit 0
  fi
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
[ "$HOOK" = 1 ] && echo "  (hook: no coverage floor, no UI build — run without --hook for those)"

# ── Python core / SDK ───────────────────────────────────────────────────────
if touched '^(polyris/|tests/|pyproject\.toml)'; then
  run "ruff"                    ruff check .
  run "mypy (py.typed contract)" python3 -m mypy polyris/ --ignore-missing-imports
  if [ "$HOOK" = 1 ]; then
    run "pytest"                python3 -m pytest tests/sdk/ tests/backend/ tests/integration/ -q -p no:cacheprovider
  else
    run "pytest + coverage floor" make test-cov
  fi
fi

# ── Docs drift (docs, and the code and template they describe) ──────────────
if touched '^(docs/|README\.md|CLAUDE\.md|CONTRIBUTING\.md|sam/template\.yaml|sam/lambdas/console_api/|polyris/|tests/docs/)'; then
  run "docs drift tests"        make test-docs
fi

# ── Lambdas / SAM ───────────────────────────────────────────────────────────
if touched '^(sam/|polyris/constants\.py|polyris/codegen/)'; then
  run "cfn-lint"                cfn-lint sam/template.yaml
  run "loggers"                 make sync-loggers
  run "enum codegen drift"      make check-generate-enums
  run "dateVars codegen drift"  make check-generate-variables
  run "SFN status literals"     make check-sfn-templates
  run "backfill parity"         make check-backfill-parity
  for d in notify query_subscriptions bucket_cleanup; do
    run "lambda: $d" bash -c "cd sam/lambdas/$d && PYTHONPATH=. python3 -m pytest -q -p no:cacheprovider"
  done
  run "console_api" bash -c "cd sam/lambdas/console_api && PYTHONPATH=. python3 -m pytest tests/ -q -p no:cacheprovider"
  run "coordination" bash -c "cd sam/lambdas/coordination && PYTHONPATH=. python3 -m pytest tests/ -q -p no:cacheprovider"
fi

# ── UI ──────────────────────────────────────────────────────────────────────
if touched '^ui/'; then
  if [ -d ui/node_modules ]; then
    run "ui: typecheck"  bash -c "cd ui && npm run typecheck"
    run "ui: eslint"     bash -c "cd ui && npm run lint"
    run "ui: vitest"     bash -c "cd ui && npx vitest run"
    [ "$HOOK" = 1 ] || run "ui: build" bash -c "cd ui && npm run build"
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
  # Remember the checkout this run saw green, unless it changed meanwhile.
  if [ -n "$PRINT" ] && [ "$(fingerprint)" = "$PRINT" ]; then
    echo "$PRINT" > "$STATE_DIR/verify-changed.green"
  fi
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
