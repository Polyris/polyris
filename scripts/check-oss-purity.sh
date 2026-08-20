#!/usr/bin/env bash
#
# OSS-purity guard: enforces CLAUDE.md Principle #24 mechanically.
#
# Fails CI if user-facing docs or examples contain "comparison phrases" that
# describe what OSS doesn't ship (violating #24) — or if any markdown link in
# a user-facing doc points at a file that doesn't exist.
#
# Two scopes:
#
#   1. USER-FACING PATHS — must be silent about non-OSS surfaces. Any hit
#      here fails the script.
#         docs/features/  docs/getting-started/  docs/deployment/
#         docs/architecture/  docs/operations/
#         README.md  examples/
#
#   2. SEAM-DEFINING PATHS — legitimately reference the OSS/paid seam because
#      they describe the mechanism, not the feature. Skipped by this check
#      (see CLAUDE.md #24 seam-defining exception).
#         docs/reference/adr-*.md
#         docs/reference/SPIKE_*.md
#         docs/reference/alerting-master-plan.md
#         CLAUDE.md  docs/CLAUDE.md
#
# The HelpModal's OSS-safe content is separately pinned by
# `ui/src/components/HelpModal.test.tsx`; run vitest for that.

set -euo pipefail
cd "$(dirname "$0")/.."

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'; NC='\033[0m'

fail=0

# ── 1. Comparison phrases in user-facing content ────────────────────────────
# Each pattern matches a shape of "OSS is a subset" advertising. The specific
# feature names inside them can change; the shape is what we ban.
FORBIDDEN_PATTERNS=(
  'not yet in the open-source'
  'not in the open-source'
  'OSS build has no'
  'OSS build does not'
  'coming to OSS'
  'available in the paid'
  'in the paid tier'
  'compared to the paid'
  'compared to the full'
  'the full version'
  'upgrade to unlock'
  'coming soon'
  # Backfill is not in the OSS build; a *content-level* mention in user-facing
  # docs is a #24 violation. The DATA_PASSING.md "safe to re-run" wording is
  # neutral; anything explicitly naming a backfill feature is not.
  '[Bb]ackfill[ _]feature'
  '[Bb]ackfill with date range'
)

# Paths that must be OSS-pure. Kept explicit rather than "everything under
# docs/" so the seam-defining paths in docs/reference/ don't trip the check.
USER_FACING_PATHS=(
  docs/features
  docs/getting-started
  docs/deployment
  docs/architecture
  docs/operations
  README.md
  examples
)

echo "── OSS purity: forbidden comparison phrases ─────────────────────────"
for pattern in "${FORBIDDEN_PATTERNS[@]}"; do
  hits=$(grep -rEnI "$pattern" "${USER_FACING_PATHS[@]}" 2>/dev/null \
    --include='*.md' --include='*.py' --include='*.tsx' --include='*.ts' \
    --exclude-dir=node_modules --exclude-dir=__pycache__ --exclude-dir=.next || true)
  if [ -n "$hits" ]; then
    echo -e "${RED}✗${NC} pattern: $pattern"
    echo "$hits" | sed 's/^/    /'
    fail=1
  fi
done
if [ $fail -eq 0 ]; then
  echo -e "${GRN}✓${NC} no forbidden phrases in user-facing paths"
fi

# ── 2. Dead links in user-facing markdown ───────────────────────────────────
# Only checks relative links (skips http(s):// and fragment-only anchors).
# Points at the actual target file existing on disk — doesn't try to follow
# section anchors.
echo ""
echo "── OSS purity: markdown link resolution ─────────────────────────────"
link_fail=0
# Collect all .md files first, then process — avoiding pipes into `while`
# (which run in a subshell and swallow variable assignments).
mapfile -t MD_FILES < <(find "${USER_FACING_PATHS[@]}" -name '*.md' 2>/dev/null)
for md_file in "${MD_FILES[@]}"; do
  # Extract every target from [text](target) — skip http(s)://, mailto:, and
  # pure fragment anchors.
  mapfile -t TARGETS < <(
    grep -oE '\]\([^)]+\)' "$md_file" 2>/dev/null | \
      sed -E 's/^\]\(//; s/\)$//'
  )
  md_dir=$(dirname "$md_file")
  for target in "${TARGETS[@]}"; do
    case "$target" in
      http*://*|mailto:*|'#'*|'') continue ;;
    esac
    target_no_frag="${target%%#*}"
    [ -z "$target_no_frag" ] && continue
    resolved="$md_dir/$target_no_frag"
    if [ ! -e "$resolved" ]; then
      echo -e "${RED}✗${NC} $md_file → $target_no_frag (not found)"
      link_fail=1
      fail=1
    fi
  done
done

if [ $link_fail -eq 0 ]; then
  echo -e "${GRN}✓${NC} all relative markdown links resolve"
fi

# ── Result ─────────────────────────────────────────────────────────────────
echo ""
if [ $fail -eq 0 ]; then
  echo -e "${GRN}✓ OSS purity check passed${NC}"
  exit 0
else
  echo -e "${RED}✗ OSS purity check failed${NC}"
  echo "  Fix the items above or extend the seam-defining exemption list in this script."
  exit 1
fi
