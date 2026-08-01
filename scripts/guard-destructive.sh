#!/usr/bin/env bash
# ── guard-destructive.sh ─────────────────────────────────────────────────────
# PreToolUse hook for Bash. Blocks the destructive patterns that have actually
# cost work here, rather than a generic denylist.
#
# The one that bit us: `rm -rf <worktree> && unzip <archive>` to "start clean"
# during a review — which discarded uncommitted fixes from earlier in the same
# session. Reading a hook's rejection is cheap; re-deriving lost work is not.
#
# Input: the tool call as JSON on stdin. Exit 2 blocks and shows the reason.
set -uo pipefail

INPUT="$(cat)"
CMD="$(printf '%s' "$INPUT" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("tool_input",{}).get("command",""))
except Exception: print("")' 2>/dev/null)"

[ -z "$CMD" ] && exit 0

block() {
  echo "BLOCKED: $1" >&2
  echo "If this is genuinely intended, ask the maintainer first." >&2
  exit 2
}

# rm -rf on a tracked working tree
case "$CMD" in
  *"rm -rf"*|*"rm -fr"*)
    case "$CMD" in
      *"/tmp/"*|*"node_modules"*|*"__pycache__"*|*".pytest_cache"*|*".ruff_cache"*|\
      *".mypy_cache"*|*".next"*|*"/out"*|*".test-merge"*|*".coverage"*)
        ;;  # cache / scratch paths are fine
      *)
        block "rm -rf outside cache/scratch paths. A working tree with uncommitted
work has been destroyed this way before. Delete specific paths, or commit first." ;;
    esac ;;
esac

# git commands that discard uncommitted work
case "$CMD" in
  *"git checkout ."*|*"git reset --hard"*|*"git clean -fd"*|*"git restore ."*)
    block "this discards uncommitted changes in the working tree." ;;
esac

# force-push
case "$CMD" in
  *"push --force"*|*"push -f "*)
    block "force-push rewrites published history." ;;
esac

exit 0
