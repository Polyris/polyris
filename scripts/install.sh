#!/usr/bin/env bash
# scripts/install.sh — install the polyris CLI (ADR #131).
#
# Usage:
#   curl -fsSL https://github.com/Polyris/polyris/releases/latest/download/install.sh | bash
#
# Then:
#   polyris-up        # install the platform into your AWS account
#
# Needs only Python >= 3.11 — no git, node, aws CLI or sam. Installs the
# latest released polyris (or POLYRIS_REF) into its own virtualenv and links
# the polyris-* commands into ~/.local/bin, so it never touches the system
# Python (works on distros that mark it "externally managed", PEP 668).
# Uses pipx instead when it is installed.
#
# Env vars:
#   POLYRIS_REF   — release tag to install (default: the latest GitHub release)
#   POLYRIS_HOME  — where the virtualenv lives (default: ~/.local/share/polyris)
#   POLYRIS_BIN   — where commands are linked (default: ~/.local/bin)
set -euo pipefail

REPO="Polyris/polyris"
POLYRIS_HOME="${POLYRIS_HOME:-$HOME/.local/share/polyris}"
POLYRIS_BIN="${POLYRIS_BIN:-$HOME/.local/bin}"

if [ -t 1 ]; then
  NC='\033[0m'; BOLD='\033[1m'; RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'; CYA='\033[0;36m'
else
  NC=''; BOLD=''; RED=''; GRN=''; YLW=''; CYA=''
fi
ok()   { printf "${GRN}✅ %s${NC}\n" "$1"; }
warn() { printf "${YLW}⚠️  %s${NC}\n" "$1"; }
fail() { printf "${RED}❌ %s${NC}\n" "$1" >&2; exit 1; }

# ── Python >= 3.11 ────────────────────────────────────────────────────────────
PYTHON=""
for candidate in python3 python3.13 python3.12 python3.11; do
  if command -v "$candidate" >/dev/null 2>&1 \
     && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PYTHON="$candidate"; break
  fi
done
if [ -z "$PYTHON" ]; then
  printf "${RED}❌ polyris needs Python 3.11 or newer.${NC}\n" >&2
  case "$(uname -s)" in
    Darwin*) echo "   brew install python@3.12" >&2 ;;
    Linux*)
      echo "   AWS CloudShell / Amazon Linux 2023: sudo dnf install -y python3.11" >&2
      echo "   Ubuntu / Debian:                    sudo apt install python3.12 python3.12-venv" >&2
      echo "   Fedora:                             sudo dnf install python3.12" >&2 ;;
    *) echo "   https://www.python.org/downloads/" >&2 ;;
  esac
  echo "   Then re-run this installer." >&2
  exit 1
fi
ok "$("$PYTHON" --version)"

# ── Version ───────────────────────────────────────────────────────────────────
REF="${POLYRIS_REF:-}"
if [ -z "$REF" ]; then
  REF="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
    | grep -E '^[[:space:]]*"tag_name"[[:space:]]*:' | head -1 \
    | sed -E 's/.*"tag_name"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/' || true)"
  [ -n "$REF" ] || fail "Could not look up the latest release (GitHub API unreachable or rate-limited). Set POLYRIS_REF=vX.Y.Z and retry."
fi
# A source archive, not git+https: pip then needs no git binary.
SOURCE="https://github.com/$REPO/archive/refs/tags/$REF.tar.gz"
printf "${BOLD}Installing polyris %s${NC}\n" "$REF"

# ── Install ───────────────────────────────────────────────────────────────────
if command -v pipx >/dev/null 2>&1; then
  pipx install --force --python "$PYTHON" "$SOURCE"
else
  "$PYTHON" -m venv "$POLYRIS_HOME/venv" \
    || fail "Could not create a virtualenv. On Debian/Ubuntu: sudo apt install python3-venv"
  "$POLYRIS_HOME/venv/bin/pip" install --quiet --upgrade pip
  "$POLYRIS_HOME/venv/bin/pip" install --quiet --upgrade "$SOURCE"
  mkdir -p "$POLYRIS_BIN"
  for cmd in "$POLYRIS_HOME"/venv/bin/polyris*; do
    ln -sf "$cmd" "$POLYRIS_BIN/$(basename "$cmd")"
  done
fi
ok "polyris $REF installed"

case ":$PATH:" in
  *":$POLYRIS_BIN:"*) ;;
  *) warn "$POLYRIS_BIN is not on your PATH. Add it:  export PATH=\"$POLYRIS_BIN:\$PATH\"" ;;
esac

echo
printf "${BOLD}Next:${NC} install the platform into your AWS account\n"
printf "  ${CYA}polyris-up${NC}            # uses your default AWS credentials; --profile / --region to override\n"
