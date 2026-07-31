#!/usr/bin/env bash
# ── check-no-paid.sh ─────────────────────────────────────────────────────────
# Fails if any proprietary path is tracked in this PUBLIC repo. The free product
# must never carry paid code: no `ee/` console routes, no `ui/src/ee/`, no
# `polyris/_ee/`. Wired into CI (.github/workflows/ci.yml) and `make check`,
# alongside ui/scripts/check-oss-build.sh which proves the free build works with
# ee/ absent.
#
# Fail-closed: a leak guard that passes when it cannot run is worse than no
# guard. If this is not a git work tree, or `git ls-files` fails, that is an
# error — not a pass.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "❌ check-no-paid: not a git work tree — cannot verify what is tracked."
  echo "   This guard must run against a checkout, not an unpacked archive."
  exit 2
fi

if ! tracked="$(git ls-files)"; then
  echo "❌ check-no-paid: 'git ls-files' failed — refusing to report success."
  exit 2
fi

paid="$(printf '%s\n' "$tracked" | grep -E '(^|/)(_ee|ee)/' || true)"

if [ -n "$paid" ]; then
  echo "❌ proprietary (paid) paths are tracked in the public repo — they belong"
  echo "   only in the private polyris-ee repo. Remove them:"
  echo "$paid" | sed 's/^/     /'
  exit 1
fi

echo "✓ no paid code in the public repo — no ee/ or _ee/ paths tracked"
