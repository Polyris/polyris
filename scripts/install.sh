#!/usr/bin/env bash
# scripts/install.sh — one-line onboarding for polyris.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Polyris/polyris/main/scripts/install.sh | bash
#
# Env vars:
#   POLYRIS_DIR   — clone destination (default: $PWD/polyris, matching
#                   the way `git clone URL` puts things in ./URL_basename)
#   POLYRIS_REF   — branch or tag to check out (git clone --branch, which
#                   does NOT accept commit SHAs). If unset, defaults to the
#                   latest GitHub release tag (queried from the API); falls
#                   back to `main` (with a warning) if there are no releases
#                   yet or the API is unreachable / rate-limited.
#
# Flow:
#   1. Verify prerequisites (git, python3, node, aws, sam). Report every
#      missing one in a single pass with per-OS install commands.
#   2. `git clone` into POLYRIS_DIR (fresh only — refuses to overwrite an
#      existing directory).
#   3. Print the one command the user runs next: `./scripts/setup-polyris.sh`.
#
# Deliberately does NOT run setup-polyris.sh itself: interactive prompts
# through `curl | bash` are fragile (stdin is consumed by the pipe) and
# the operator should see where the repo landed before we start asking
# questions. Two-step is the safer UX.
set -euo pipefail

INSTALL_DIR="${POLYRIS_DIR:-$PWD/polyris}"
REPO_URL="https://github.com/Polyris/polyris.git"
REPO_API="https://api.github.com/repos/Polyris/polyris"
SELF_URL="https://raw.githubusercontent.com/Polyris/polyris/main/scripts/install.sh"

# Resolve the default POLYRIS_REF to the latest GitHub release tag, so a
# fresh curl-install gives a reproducible, released version of polyris
# rather than whatever main happens to be right now. If the user set
# POLYRIS_REF explicitly (branch, tag, or commit sha), respect that.
# If the API call fails (offline, rate-limited, misconfigured token, …),
# fall back to main so the installer stays usable — better a working
# install off main than a broken installer.
_latest_tag() {
  # ``releases/latest`` returns the newest non-prerelease tag by default.
  # curl -f exits non-zero on 4xx/5xx so failures propagate cleanly.
  curl -fsSL "$REPO_API/releases/latest" 2>/dev/null \
    | grep -E '^\s*"tag_name"\s*:' \
    | head -1 \
    | sed -E 's/.*"tag_name"\s*:\s*"([^"]+)".*/\1/'
}

POLYRIS_REF_FALLBACK=""
if [ -z "${POLYRIS_REF:-}" ]; then
  POLYRIS_REF="$(_latest_tag || true)"
  if [ -z "$POLYRIS_REF" ]; then
    POLYRIS_REF="main"
    POLYRIS_REF_FALLBACK="1"
  fi
fi

# ── UI helpers ────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
  NC='\033[0m'; BOLD='\033[1m'; RED='\033[0;31m'; GRN='\033[0;32m'
  YLW='\033[0;33m'; CYA='\033[0;36m'
else
  NC=''; BOLD=''; RED=''; GRN=''; YLW=''; CYA=''
fi

hdr()  { printf "\n${BOLD}${CYA}── %s ──────────────────────────────────────${NC}\n" "$1"; }
ok()   { printf "${GRN}✅ %s${NC}\n" "$1"; }
warn() { printf "${YLW}⚠️  %s${NC}\n" "$1"; }
fail() { printf "${RED}❌ %s${NC}\n" "$1" >&2; exit 1; }

# ── OS detection ──────────────────────────────────────────────────────────────
# Only used to pick the right install command for missing tools. Falls
# back to generic docs URLs for anything unusual.
case "$(uname -s)" in
  Darwin*) _OS=darwin ;;
  Linux*)  _OS=linux ;;
  *)       _OS=other ;;
esac

# ── Prereq check ──────────────────────────────────────────────────────────────
# Collect every missing tool (never bail on the first), then print all
# with copy-pasteable install commands so the operator can fix everything
# in one round-trip before re-running the installer.
_hint_for() {
  case "$1" in
    git)
      case "$_OS" in
        darwin) echo "     xcode-select --install   # or: brew install git" ;;
        linux)  echo "     Ubuntu/Debian: sudo apt install git"
                echo "     Fedora:        sudo dnf install git" ;;
        *)      echo "     https://git-scm.com/downloads" ;;
      esac
      ;;
    python3)
      case "$_OS" in
        darwin) echo "     brew install python@3.12" ;;
        linux)  echo "     Ubuntu 24.04+ / Debian 12+ / Fedora 40+ ship it."
                echo "     Older distros: use pyenv (https://github.com/pyenv/pyenv#installation)" ;;
        *)      echo "     https://www.python.org/downloads/  (need ≥ 3.11)" ;;
      esac
      ;;
    node)
      echo "     Install Node.js ≥ 20.19 via nvm:"
      echo "       curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/master/install.sh | bash"
      echo "       source ~/.nvm/nvm.sh && nvm install 22"
      ;;
    aws)
      case "$_OS" in
        darwin) echo "     brew install awscli" ;;
        linux)  echo "     curl 'https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip' -o /tmp/awscliv2.zip \\"
                echo "       && unzip /tmp/awscliv2.zip -d /tmp && sudo /tmp/aws/install" ;;
        *)      echo "     https://aws.amazon.com/cli/" ;;
      esac
      ;;
    sam)
      case "$_OS" in
        darwin) echo "     brew tap aws/tap && brew install aws-sam-cli" ;;
        linux)  echo "     pip install aws-sam-cli   # (requires python 3)" ;;
        *)      echo "     https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html" ;;
      esac
      ;;
  esac
}

hdr "Polyris installer"
echo "  Cloning to:    $INSTALL_DIR"
echo "  Branch/tag:    $POLYRIS_REF"
if [ -n "$POLYRIS_REF_FALLBACK" ]; then
  warn "No GitHub release tag found (API unreachable, rate-limited, or no releases yet)."
  warn "Falling back to \`main\`. Set POLYRIS_REF=<tag> to pin a released version."
fi
echo

hdr "Checking prerequisites"
missing=()
for tool in git python3 node aws sam; do
  if command -v "$tool" >/dev/null 2>&1; then
    ok "$tool"
  else
    printf "${RED}❌ %s not found${NC}\n" "$tool"
    missing+=("$tool")
  fi
done

if [ ${#missing[@]} -gt 0 ]; then
  echo
  printf "${RED}❌ Install the missing tools above, then re-run:${NC}\n" >&2
  printf "   curl -fsSL %s | bash\n" "$SELF_URL" >&2
  echo >&2
  for tool in "${missing[@]}"; do
    printf "${BOLD}%s${NC}\n" "$tool" >&2
    _hint_for "$tool" >&2
    echo >&2
  done
  exit 1
fi

# ── Clone ─────────────────────────────────────────────────────────────────────
# Refuse to touch an existing path — clobbering someone's working copy is a
# much worse mistake than the two seconds it takes to pass POLYRIS_DIR.
if [ -e "$INSTALL_DIR" ]; then
  fail "$INSTALL_DIR already exists.
   Move it, delete it, or choose another location:
     curl -fsSL $SELF_URL | POLYRIS_DIR=~/other-polyris bash"
fi

hdr "Cloning polyris → $INSTALL_DIR"
git clone --depth=1 --branch="$POLYRIS_REF" "$REPO_URL" "$INSTALL_DIR"
ok "Cloned"

# ── Next step ─────────────────────────────────────────────────────────────────
hdr "Next step"
echo "  cd $INSTALL_DIR"
echo "  ./scripts/setup-polyris.sh"
echo
echo "  setup-polyris.sh walks through the interactive install, deploys the"
echo "  SAM infra + UI, and scaffolds a first pipeline. It also supports"
echo "  --create-user and --delete for later — see the header of that file."
