#!/usr/bin/env bash
#
# Open-core OSS-build guard (ADR #99).
#
# Proves the public UI build is self-contained: with the paid surface (src/ee,
# all tiers — team + enterprise, ADR #100) removed, the app must still regenerate
# its EE-active stub, typecheck, and build. A stray `@/ee/…` import from free
# code — or a free→ee type leak — fails here, before it can ship in the public repo.
#
# Safe to run locally (restores src/ee on exit) and in CI (fresh checkout).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d src/ee ]; then
  echo "src/ee not present — already OSS layout; running stub build only."
fi

BAK=""
restore() {
  if [ -n "$BAK" ] && [ -d "$BAK/ee" ]; then
    rm -rf src/ee
    mv "$BAK/ee" src/ee
  fi
  node scripts/gen-ee-active.mjs >/dev/null 2>&1 || true
}
trap restore EXIT

if [ -d src/ee ]; then
  BAK="$(mktemp -d)"
  mv src/ee "$BAK/ee"
fi

echo "→ regenerating EE-active module (src/ee absent → empty stub)"
node scripts/gen-ee-active.mjs

echo "→ typecheck (OSS)"
npx tsc --noEmit

echo "→ build (OSS)"
npm run build >/dev/null

echo "✓ OSS build is self-contained — no free→ee imports or type leaks."
