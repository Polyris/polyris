#!/usr/bin/env bash
# scripts/setup-polyris.sh — bootstrap / teardown / user-management for polyris.
#
# For the monorepo case (SAM + UI + pipelines in one checkout).
#
#   ./scripts/setup-polyris.sh                                          # interactive install
#   ./scripts/setup-polyris.sh --delete [--stack X --region Y --profile Z]      # tear down
#   ./scripts/setup-polyris.sh --create-user [--stack X --region Y --profile Z] # add a user
#
# For --delete and --create-user, flags override; missing values default to
# sam/samconfig.toml (if present); anything still missing is prompted.
#
# INSTALL flow:
#   * asks a handful of questions (profile, region, namespace, stage, …)
#   * writes sam/samconfig.toml and pipelines/config.py
#   * `pip install -e .` for the polyris SDK — gives the user polyris-init /
#     polyris-deploy for the pipeline step
#   * runs `sam build && sam deploy`
#   * builds and deploys the UI (explicit positional args to ui/deploy.sh)
#   * scaffolds pipelines/hello-world/dag.py (a minimal working pipeline
#     pointing at the built-in TestQuick SFN)
#   * (only if Cognito auth is enabled) creates the first admin user AFTER
#     the UI is live — no auth means no user is needed
#
# DELETE flow (--delete):
#   * resolves stack/region/profile (CLI flags → samconfig.toml → prompt)
#   * describe-stacks first; if already gone, exits ok
#   * confirms, runs `sam delete` in the background (BucketCleanup empties
#     the buckets first), and polls describe-stack-events in the foreground
#     so the operator sees per-resource progress
#
# CREATE-USER flow (--create-user):
#   * resolves stack/region/profile (CLI flags → samconfig.toml → prompt)
#   * looks up the Cognito User Pool from CFN outputs
#   * asks for email/username and whether to set a permanent password
#   * calls admin-create-user with --message-action SUPPRESS (no welcome
#     email); if permanent was chosen, follows with admin-set-user-password
#
# Rerunning install is safe — every write step prompts before overwriting
# an existing file. Any step failure aborts the script.
set -euo pipefail

# ── UI helpers ────────────────────────────────────────────────────────────────
NC='\033[0m'; BOLD='\033[1m'; RED='\033[0;31m'; GRN='\033[0;32m'
YLW='\033[0;33m'; CYA='\033[0;36m'

hdr()  { printf "\n${BOLD}${CYA}── %s ──────────────────────────────────────${NC}\n" "$1"; }
ok()   { printf "${GRN}✅ %s${NC}\n" "$1"; }
warn() { printf "${YLW}⚠️  %s${NC}\n" "$1"; }
fail() { printf "${RED}❌ %s${NC}\n" "$1" >&2; exit 1; }

ask() {
  # ask VAR "Prompt" [default]
  # Requires a TTY on stdin when no default is provided — under `curl | bash`
  # or any other pipe, `read` returns empty immediately and would infinite-loop
  # the "required prompt" branch. Fail fast with an actionable message instead.
  local __var="$1" __prompt="$2" __default="${3:-}"
  local __input
  if [ -n "$__default" ]; then
    if [ -t 0 ]; then
      read -r -p "  $__prompt [$__default]: " __input
    else
      __input=""
    fi
    printf -v "$__var" '%s' "${__input:-$__default}"
  else
    if [ ! -t 0 ]; then
      fail "This step needs an interactive answer for \"$__prompt\", but stdin is not a terminal.
   Run this script directly (\`./scripts/setup-polyris.sh\`), not through a pipe."
    fi
    while [ -z "${__input:-}" ]; do
      read -r -p "  $__prompt: " __input
    done
    printf -v "$__var" '%s' "$__input"
  fi
}

confirm() {
  # confirm "Prompt" [Y|N default]
  # Requires a TTY: a piped `bash` would silently accept the default and could
  # trigger a destructive path (e.g. overwrite prompt on --delete). Fail fast.
  local prompt="$1" default="${2:-N}" input
  local suffix; if [ "$default" = "Y" ]; then suffix="[Y/n]"; else suffix="[y/N]"; fi
  if [ ! -t 0 ]; then
    fail "\"$prompt\" needs an interactive yes/no, but stdin is not a terminal.
   Run this script directly (\`./scripts/setup-polyris.sh\`), not through a pipe."
  fi
  read -r -p "  $prompt $suffix: " input
  input="${input:-$default}"
  [[ "$input" =~ ^[Yy]$ ]]
}

# ── Preflight (shared) ────────────────────────────────────────────────────────
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Detect OS so ``_check_prereqs`` can print install commands the user can
# actually paste. Anything other than ``darwin`` / ``linux`` falls back to
# generic docs links.
case "$(uname -s)" in
  Darwin*) _OS=darwin ;;
  Linux*)  _OS=linux ;;
  *)       _OS=other ;;
esac

# _check_prereqs "tool1 tool2 ..." — verifies every listed tool is on PATH.
# Missing tools are collected (never fail on the first), then all reported
# together with per-OS install commands. Exits non-zero only after the full
# list so the user can install everything in one go and re-run once.
_check_prereqs() {
  local missing=()
  local tool
  for tool in "$@"; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      missing+=("$tool")
    fi
  done
  [ ${#missing[@]} -eq 0 ] && return 0

  echo
  printf "${RED}❌ Missing required tools: %s${NC}\n" "${missing[*]}" >&2
  echo   "   Install them and re-run this script." >&2
  echo >&2
  for tool in "${missing[@]}"; do
    printf "${BOLD}   %s${NC}\n" "$tool" >&2
    case "$tool" in
      aws)
        case "$_OS" in
          darwin) echo "     brew install awscli" >&2 ;;
          linux)  echo "     curl 'https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip' -o /tmp/awscliv2.zip \\" >&2
                  echo "       && unzip /tmp/awscliv2.zip -d /tmp && sudo /tmp/aws/install" >&2 ;;
          *)      echo "     https://aws.amazon.com/cli/" >&2 ;;
        esac
        ;;
      sam)
        case "$_OS" in
          darwin) echo "     brew tap aws/tap && brew install aws-sam-cli" >&2 ;;
          linux)  echo "     pip install aws-sam-cli   # (requires python 3)" >&2 ;;
          *)      echo "     https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html" >&2 ;;
        esac
        ;;
      node|npm)
        echo "     Install Node.js ≥ 20.19 via nvm (recommended):" >&2
        echo "       curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/master/install.sh | bash" >&2
        echo "       source ~/.nvm/nvm.sh && nvm install 22" >&2
        ;;
      python3)
        case "$_OS" in
          darwin) echo "     brew install python@3.12" >&2 ;;
          linux)  echo "     Ubuntu 24.04+ / Debian 12+ / Fedora 40+ already ship it." >&2
                  echo "     Older distros: use pyenv (https://github.com/pyenv/pyenv#installation)" >&2 ;;
          *)      echo "     https://www.python.org/downloads/  (need ≥ 3.11)" >&2 ;;
        esac
        ;;
      pip|pip3)
        case "$_OS" in
          darwin) echo "     Bundled with python3 (from brew install python@3.12)" >&2 ;;
          linux)  echo "     Ubuntu/Debian: sudo apt install python3-pip" >&2
                  echo "     Fedora:        sudo dnf install python3-pip" >&2 ;;
          *)      echo "     Bundled with Python; if missing, see https://pip.pypa.io/en/stable/installation/" >&2 ;;
        esac
        ;;
      git)
        case "$_OS" in
          darwin) echo "     xcode-select --install   # or: brew install git" >&2 ;;
          linux)  echo "     Ubuntu/Debian: sudo apt install git" >&2
                  echo "     Fedora:        sudo dnf install git" >&2 ;;
          *)      echo "     https://git-scm.com/downloads" >&2 ;;
        esac
        ;;
      *)
        echo "     (no built-in install hint — see the tool's website)" >&2
        ;;
    esac
    echo >&2
  done
  exit 1
}

# aws + sam are needed by every mode (install / --delete / --create-user).
_check_prereqs aws sam

[ -d sam ] || fail "sam/ directory missing — run this from the polyris repo root."

# Best-effort read of stack_name / region / profile from sam/samconfig.toml
# into ``SAMCFG_STACK`` / ``SAMCFG_REGION`` / ``SAMCFG_PROFILE`` — leaves them
# empty when the file (or a field) is missing, never fails. The --delete
# and --create-user modes use this as a source of *defaults* and prompt
# for anything still empty; they must not require the file to exist so a
# user can operate on any stack from any checkout.
_maybe_read_samconfig() {
  SAMCFG_STACK=""; SAMCFG_REGION=""; SAMCFG_PROFILE=""
  local f="sam/samconfig.toml"
  [ -f "$f" ] || return 0
  SAMCFG_STACK="$(grep -E '^\s*stack_name\s*=' "$f" | head -1 | sed 's/^[^"]*"\([^"]*\)".*/\1/')"
  SAMCFG_REGION="$(grep -E '^\s*region\s*=' "$f"   | head -1 | sed 's/^[^"]*"\([^"]*\)".*/\1/')"
  SAMCFG_PROFILE="$(grep -E '^\s*profile\s*=' "$f" | head -1 | sed 's/^[^"]*"\([^"]*\)".*/\1/')"
}

# Resolve stack / region / profile from (in order): CLI flags passed to this
# subcommand, sam/samconfig.toml, interactive prompt. Populates
# ``STACK_NAME`` / ``REGION`` / ``PROFILE`` for the caller. Consumes the
# rest of the args in $@ (so caller shifts $1 first).
_resolve_target() {
  local flag_stack="" flag_region="" flag_profile=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --stack)     flag_stack="${2:-}";   shift 2 2>/dev/null || shift ;;
      --stack=*)   flag_stack="${1#*=}";  shift ;;
      --region)    flag_region="${2:-}";  shift 2 2>/dev/null || shift ;;
      --region=*)  flag_region="${1#*=}"; shift ;;
      --profile)   flag_profile="${2:-}"; shift 2 2>/dev/null || shift ;;
      --profile=*) flag_profile="${1#*=}"; shift ;;
      *)           fail "Unknown flag: $1 (accepts --stack / --region / --profile)" ;;
    esac
  done

  _maybe_read_samconfig
  STACK_NAME="${flag_stack:-$SAMCFG_STACK}"
  REGION="${flag_region:-$SAMCFG_REGION}"
  PROFILE="${flag_profile:-$SAMCFG_PROFILE}"

  hdr "Target"
  [ -n "$STACK_NAME" ] || ask STACK_NAME "SAM CloudFormation stack name"
  [ -n "$REGION" ]     || ask REGION     "AWS region"           "us-east-1"
  [ -n "$PROFILE" ]    || ask PROFILE    "AWS profile"          "default"
  # If any came from samconfig without prompting, show what we resolved so
  # the operator can spot a wrong stack before we touch it.
  echo "  Resolved: stack=$STACK_NAME  region=$REGION  profile=$PROFILE"
}

# ═══════════════════════════════════════════════════════════════════════════════
# CREATE-USER MODE
# ═══════════════════════════════════════════════════════════════════════════════
if [ "${1:-}" = "--create-user" ]; then
  hdr "Create Cognito user"
  shift  # consume --create-user before parsing target flags
  _resolve_target "$@"

  POOL_ID="$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" --region "$REGION" --profile "$PROFILE" \
    --query "Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId'].OutputValue" \
    --output text 2>/dev/null || true)"

  if [ -z "$POOL_ID" ] || [ "$POOL_ID" = "None" ]; then
    fail "No CognitoUserPoolId output on stack '$STACK_NAME'. Was the stack \
deployed with EnableCognitoAuth=true?"
  fi
  ok "User pool: $POOL_ID"

  hdr "User details"
  ask NEW_EMAIL    "Email"
  ask NEW_USERNAME "Username" "$NEW_EMAIL"

  # Two flows: temp password (user changes on first login) vs permanent password.
  # Temp is the safer default — no shell history exposure of a real password.
  echo
  if confirm "Set a PERMANENT password now? (No = generate a temp password, user changes on first login)" "N"; then
    while :; do
      read -r -sp "  Password (min 8 chars, mix upper/lower/digit/symbol): " NEW_PW; echo
      read -r -sp "  Repeat:   " NEW_PW2; echo
      if [ "$NEW_PW" = "$NEW_PW2" ] && [ "${#NEW_PW}" -ge 8 ]; then break; fi
      warn "Passwords don't match or too short. Try again."
    done
    PERMANENT=1
  else
    # Reasonably-strong random with symbols so it matches Cognito's default policy.
    NEW_PW="TempPass-$(head -c 6 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 6)!1"
    PERMANENT=0
  fi

  hdr "Creating user"
  # admin-create-user always sets a temporary password + MessageAction=SUPPRESS
  # so nothing goes out over email (we don't want AWS to send the user random
  # links).
  aws cognito-idp admin-create-user \
    --user-pool-id "$POOL_ID" \
    --username "$NEW_USERNAME" \
    --user-attributes "Name=email,Value=$NEW_EMAIL" "Name=email_verified,Value=true" \
    --temporary-password "$NEW_PW" \
    --message-action SUPPRESS \
    --region "$REGION" --profile "$PROFILE" \
    >/dev/null
  ok "User created"

  if [ "$PERMANENT" = "1" ]; then
    aws cognito-idp admin-set-user-password \
      --user-pool-id "$POOL_ID" \
      --username "$NEW_USERNAME" \
      --password "$NEW_PW" \
      --permanent \
      --region "$REGION" --profile "$PROFILE" \
      >/dev/null
    ok "Password set as permanent — user can log in immediately"
  fi

  hdr "Done"
  echo "  username:     $NEW_USERNAME"
  echo "  email:        $NEW_EMAIL"
  if [ "$PERMANENT" = "1" ]; then
    echo "  password:     (as you entered)"
  else
    echo "  temp password: $NEW_PW  (user must change on first login)"
  fi
  exit 0
fi

# ═══════════════════════════════════════════════════════════════════════════════
# DELETE MODE
# ═══════════════════════════════════════════════════════════════════════════════
if [ "${1:-}" = "--delete" ]; then
  hdr "Polyris teardown"
  shift  # consume --delete before parsing target flags
  _resolve_target "$@"

  # Detect an already-gone stack so a second `--delete` prints a friendly
  # notice instead of an obscure AWS error and a non-zero exit.
  if ! aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" --region "$REGION" --profile "$PROFILE" \
        >/dev/null 2>&1; then
    ok "Stack '$STACK_NAME' is already gone — nothing to do."
    exit 0
  fi

  echo
  warn "\`sam delete\` on '$STACK_NAME':"
  warn "  - If \`AutoEmptyBucketsOnDelete=true\` at deploy time: ResultsBucket +"
  warn "    ConsoleUiBucket are emptied by BucketCleanup (task results lost)."
  warn "  - Otherwise: sam delete will fail on non-empty buckets — empty them"
  warn "    manually first (\`aws s3 rm --recursive s3://<bucket>\`)."
  echo
  if ! confirm "Delete stack '$STACK_NAME'?" "N"; then
    fail "Aborted."
  fi

  hdr "Deleting SAM stack (10-15 min)"
  # sam delete runs the BucketCleanup Lambda first (empties both buckets),
  # then removes every resource. Fails loudly if anything else references
  # the buckets or the stack is already gone.
  #
  # sam delete blocks until done and prints only S3-object deletions +
  # "Deleting Cloudformation stack" — nothing on the ~10-15 min of
  # resource-by-resource CFN work. Run it in the background, redirect its
  # output to a log, and poll ``describe-stack-events`` in the foreground
  # so the operator can actually see progress. Mirrors what polyris-deploy
  # does on the deploy side (see polyris/deploy.py::_watch_stack_events).
  _sam_log="$(mktemp -t polyris-sam-delete.XXXXXX)"
  # Track which CFN events we've printed by EventId — the only correct way,
  # since timestamps repeat (multiple resources delete in the same second)
  # and a "since last timestamp" filter drops events.
  _seen_ids_file="$(mktemp -t polyris-sam-delete-ids.XXXXXX)"
  trap 'rm -f "$_sam_log" "$_seen_ids_file"' EXIT

  (cd sam && sam delete --stack-name "$STACK_NAME" --region "$REGION" \
                          --profile "$PROFILE" --no-prompts) \
    >"$_sam_log" 2>&1 &
  _sam_pid=$!

  _start=$SECONDS
  while kill -0 "$_sam_pid" 2>/dev/null; do
    # describe-stack-events fails once the stack is gone — swallow that;
    # we detect completion by the sam_pid finishing, not by this error.
    _events="$(aws cloudformation describe-stack-events \
        --stack-name "$STACK_NAME" --region "$REGION" --profile "$PROFILE" \
        --query 'reverse(StackEvents[].[EventId,LogicalResourceId,ResourceType,ResourceStatus,ResourceStatusReason])' \
        --output text 2>/dev/null || true)"

    if [ -n "$_events" ]; then
      while IFS=$'\t' read -r _eid _lid _rtype _rstatus _reason; do
        [ -n "$_eid" ] || continue
        if ! grep -qxF "$_eid" "$_seen_ids_file" 2>/dev/null; then
          printf '%s\n' "$_eid" >> "$_seen_ids_file"
          _elapsed=$(( SECONDS - _start ))
          printf "  [%3ds] %-40s %-32s %s\n" \
            "$_elapsed" "${_lid:-}" "${_rstatus:-}" "${_reason:--}"
        fi
      done <<< "$_events"
    fi
    sleep 5
  done

  # Reap and honour sam delete's exit code (BucketCleanup failure, etc.).
  if ! wait "$_sam_pid"; then
    echo
    warn "sam delete exited non-zero. Its log:"
    cat "$_sam_log" >&2
    fail "Delete failed — see log above."
  fi
  ok "Stack deleted"

  hdr "Done"
  echo "  Local files kept: sam/samconfig.toml, pipelines/config.py,"
  echo "                    and any pipeline dirs under pipelines/."
  echo "  Remove them by hand if you don't want to redeploy later."
  exit 0
fi

# ═══════════════════════════════════════════════════════════════════════════════
# INSTALL MODE
# ═══════════════════════════════════════════════════════════════════════════════

# Install-only preflight
# Install mode needs the full toolchain. Check them together so the user
# sees every missing one in a single pass.
_check_prereqs node npm python3
command -v pip >/dev/null || command -v pip3 >/dev/null || _check_prereqs pip
[ -d ui ] || fail "ui/ directory missing — run this from the polyris repo root."
[ -f pyproject.toml ] || fail "pyproject.toml missing — run this from the polyris repo root."

hdr "Polyris monorepo bootstrap"
echo "  This walks through a fresh install. Rerunning is safe."
echo

# ── Questions ─────────────────────────────────────────────────────────────────
hdr "Environment"
ask AWS_PROFILE_IN "AWS profile" "default"

# Verify the profile actually works before we spend 10 minutes deploying.
echo "  Verifying credentials …"
if ! aws sts get-caller-identity --profile "$AWS_PROFILE_IN" >/dev/null 2>&1; then
  fail "AWS profile '$AWS_PROFILE_IN' has no working credentials. Run \`aws configure --profile $AWS_PROFILE_IN\`."
fi
ACCOUNT_ID="$(aws sts get-caller-identity --profile "$AWS_PROFILE_IN" --query Account --output text)"
ok "Credentials OK (account $ACCOUNT_ID)"

ask REGION    "AWS region"                     "us-east-1"
ask NAMESPACE "Namespace (resource prefix)"    "myorg"
ask STAGE     "Stage name (dev, prod, …)"      "dev"

# Guardrail: Namespace+Stage must be ≤ 25 chars because the longest IAM
# role name in the template is 39 chars of suffix + 2 dashes = 41; the AWS
# roleName limit is 64. Catching this here saves a 5-minute CFN rollback.
combined_len=$(( ${#NAMESPACE} + ${#STAGE} ))
if [ "$combined_len" -gt 25 ]; then
  fail "Namespace + Stage = $combined_len chars (>25). Shorten one of them.
   Reason: the longest IAM role built from these is
   \"\${Namespace}-\${Stage}-polyris-notify-asset-subscribers-role\" — AWS caps roleName at 64."
fi

ask STACK_NAME "SAM CloudFormation stack name" "${NAMESPACE}-${STAGE}"

hdr "Console UI"
if confirm "Enable Cognito authentication for the console?" "Y"; then
  ENABLE_COGNITO="true"
  ask ADMIN_EMAIL    "First admin email"
  ask ADMIN_USERNAME "First admin username" "$ADMIN_EMAIL"
else
  ENABLE_COGNITO="false"
  ADMIN_EMAIL=""
  ADMIN_USERNAME=""
fi

hdr "Teardown behaviour"
# AutoEmptyBucketsOnDelete gates a BucketCleanup Lambda that wipes ResultsBucket
# and ConsoleUiBucket on `sam delete`. Convenient for dev/test where the whole
# stack is disposable. Dangerous for prod — a `sam delete` (accidental or not)
# is unrecoverable. Default N so an operator running this on a prod-like AWS
# account doesn't get destructive behaviour without opting in.
if confirm "Auto-empty S3 buckets on \`sam delete\` (dev/test only)?" "N"; then
  AUTO_EMPTY_BUCKETS="true"
else
  AUTO_EMPTY_BUCKETS="false"
fi

# ── Write samconfig.toml ──────────────────────────────────────────────────────
hdr "Writing sam/samconfig.toml"
SAMCONFIG="sam/samconfig.toml"
if [ -f "$SAMCONFIG" ]; then
  warn "$SAMCONFIG already exists."
  if confirm "Overwrite?" "N"; then :; else fail "Aborted — leave the existing file in place."; fi
fi

cat > "$SAMCONFIG" <<EOF
# Generated by scripts/setup-polyris.sh — feel free to hand-edit afterwards.
version = 0.1

[default.deploy.parameters]
stack_name        = "$STACK_NAME"
region            = "$REGION"
profile           = "$AWS_PROFILE_IN"
capabilities      = "CAPABILITY_IAM CAPABILITY_NAMED_IAM"
resolve_s3        = true
confirm_changeset = false

parameter_overrides = [
  "Namespace=$NAMESPACE",
  "Stage=$STAGE",
  "AwsRegion=$REGION",
  "EnableCognitoAuth=$ENABLE_COGNITO",
  # Gates the BucketCleanup Lambda that empties ResultsBucket + ConsoleUiBucket
  # on \`sam delete\`. Set to true only for dev/test — a delete then silently
  # wipes every task result. This value was chosen interactively during setup.
  "AutoEmptyBucketsOnDelete=$AUTO_EMPTY_BUCKETS",
]
EOF
ok "sam/samconfig.toml written"

# ── Write pipelines/config.py ────────────────────────────────────────────────
# Lives under pipelines/, not the repo root, because config.py is a
# *pipeline* concern — polyris-deploy walks up from each pipeline directory
# to find it. Bonus: /pipelines/ is already gitignored (real account_id
# never gets committed).
hdr "Writing pipelines/config.py"
mkdir -p pipelines
CONFIG_PY="pipelines/config.py"
SKIP_CONFIG_PY=0
if [ -f "$CONFIG_PY" ]; then
  warn "$CONFIG_PY already exists."
  if confirm "Overwrite?" "N"; then :; else warn "Leaving existing $CONFIG_PY in place."; SKIP_CONFIG_PY=1; fi
fi

if [ "$SKIP_CONFIG_PY" != "1" ]; then
  cat > "$CONFIG_PY" <<EOF
# Generated by scripts/setup-polyris.sh
# The dict KEYS ("$STAGE") are the stage names — that's what --stage picks.
ENVIRONMENTS = {
    "$STAGE": {
        "stack_name": "$STACK_NAME",   # matches sam/samconfig.toml
        "namespace":  "$NAMESPACE",
        "region":     "$REGION",
        "account_id": "$ACCOUNT_ID",
        "profile":    "$AWS_PROFILE_IN",
        "roles":      {},
    },
}

DEFAULT_STAGE = "$STAGE"
EOF
  ok "$CONFIG_PY written"
fi

# ── Install the polyris SDK (editable) ────────────────────────────────────────
# Always install editable from THIS checkout — even if a `polyris` is already
# importable, that might be an older pinned release from PyPI or an editable
# install pointing at a different checkout. `pip install -e .` is idempotent
# for the "already correct" case (fast metadata refresh) and corrects the
# rest, so a blanket install is the right move.
hdr "Installing polyris SDK (editable)"
# Prefer pip3 to avoid the pip-for-py2 ambiguity on some distros.
PIP="$(command -v pip3 || command -v pip)"

# Modern distros (Debian 12, Ubuntu 24.04, Fedora 40+) mark the system
# Python as "externally managed" and reject `pip install` outside a venv.
# Detect that up front so we don't blow up mid-deploy.
if [ -z "${VIRTUAL_ENV:-}" ] && ! "$PIP" install --dry-run -e . >/dev/null 2>&1; then
  _pip_err="$("$PIP" install --dry-run -e . 2>&1 || true)"
  if grep -q "externally-managed-environment" <<<"$_pip_err"; then
    fail "System Python is externally managed and no virtualenv is active.
   Create one and re-run:
       python3 -m venv .venv
       source .venv/bin/activate
       ./scripts/setup-polyris.sh
   (Or install polyris manually with your preferred tool, then re-run.)"
  fi
fi

"$PIP" install -e . --quiet
ok "polyris SDK installed (editable, from this checkout)"

# ── SAM build + deploy ────────────────────────────────────────────────────────
hdr "Building SAM infrastructure"
(cd sam && sam build)
ok "SAM build complete"

hdr "Deploying SAM (~10-15 min on first run)"
(cd sam && sam deploy)
ok "SAM deploy complete"

# ── UI build + deploy ─────────────────────────────────────────────────────────
hdr "Building UI"
(cd ui && npm ci && npm run build)
ok "UI build complete"

hdr "Deploying UI to S3 + CloudFront"
# Explicit positional args — ui/deploy.sh doesn't need to know about
# config.py or --stage; the setup script already has every value.
(cd ui && ./deploy.sh "$STACK_NAME" "$REGION" ./out --profile "$AWS_PROFILE_IN")
ok "UI deployed"

# ── Scaffold pipelines/hello-world/dag.py ─────────────────────────────────────
# A minimal, immediately-runnable pipeline that points at the TestQuick SFN
# the SAM stack ships. The user can `cd pipelines/hello-world && polyris-deploy`
# straight away and see the run in the Console. The ARN is composed
# deterministically from ${Namespace}-${Stage} rather than fetched, so the
# scaffold works even if CFN outputs haven't propagated yet.
hdr "Scaffolding pipelines/hello-world/dag.py"
mkdir -p pipelines/hello-world
if [ -f pipelines/hello-world/dag.py ]; then
  warn "pipelines/hello-world/dag.py already exists — leaving it alone."
else
  cat > pipelines/hello-world/dag.py <<EOF
"""hello-world — minimal working pipeline scaffolded by setup-polyris.sh.

Deploy:
    cd pipelines/hello-world
    polyris-deploy

Then open the Console URL, find hello-world, and click Run. It calls the
built-in test SFN the SAM stack ships (${NAMESPACE}-${STAGE}-polyris-test-quick),
which succeeds after ~1 second — enough to verify the whole pipeline
lifecycle (register → run → succeed → notify) is wired up correctly.
"""
from polyris import DAG, task


with DAG(
    dag_id="hello-world",
    schedule=None,           # manual trigger only — no cron/rate
    description="Hello world — verifies the platform is wired up correctly",
) as dag:

    @task.sfn(
        arn="arn:aws:states:${REGION}:${ACCOUNT_ID}:stateMachine:${NAMESPACE}-${STAGE}-polyris-test-quick",
    )
    def hello():
        """Runs the built-in test SFN (succeeds after ~1 second)."""
        pass

    hello()
EOF
  ok "pipelines/hello-world/dag.py written"
fi

# ── Cognito admin user (only if auth is enabled) ──────────────────────────────
# Do this AFTER the UI is live — a user with nowhere to log in is noise.
# When auth is disabled, no user is needed at all.
if [ "$ENABLE_COGNITO" = "true" ]; then
  hdr "Creating first Cognito user"

  POOL_ID="$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" --region "$REGION" --profile "$AWS_PROFILE_IN" \
    --query "Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId'].OutputValue" \
    --output text)"

  if [ -z "$POOL_ID" ] || [ "$POOL_ID" = "None" ]; then
    warn "CognitoUserPoolId output not found — did EnableCognitoAuth take effect?"
  else
    TEMP_PW="TempPass-$(head -c 6 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 6)!1"
    # SUPPRESS the welcome email — the user is standing at their terminal
    # right now, the temp password appears in the summary below. Cognito's
    # default email would send that password to the admin's inbox on a
    # random schedule, which is both spammy and less secure.
    aws cognito-idp admin-create-user \
      --user-pool-id "$POOL_ID" \
      --username "$ADMIN_USERNAME" \
      --user-attributes "Name=email,Value=$ADMIN_EMAIL" "Name=email_verified,Value=true" \
      --temporary-password "$TEMP_PW" \
      --message-action SUPPRESS \
      --region "$REGION" --profile "$AWS_PROFILE_IN" \
      >/dev/null
    ok "Cognito user created"
    echo "     username:      $ADMIN_USERNAME"
    echo "     email:         $ADMIN_EMAIL"
    echo "     temp password: $TEMP_PW  (change on first login)"
  fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
CONSOLE_URL="$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" --profile "$AWS_PROFILE_IN" \
  --query "Stacks[0].Outputs[?OutputKey=='ConsoleUiUrl'].OutputValue" \
  --output text)"
API_URL="$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" --profile "$AWS_PROFILE_IN" \
  --query "Stacks[0].Outputs[?OutputKey=='ConsoleApiUrl'].OutputValue" \
  --output text)"

hdr "All set"
echo "  Stack:    $STACK_NAME  ($REGION, account $ACCOUNT_ID)"
echo "  Console:  $CONSOLE_URL"
echo "  API:      $API_URL"
if [ "$ENABLE_COGNITO" = "true" ] && [ -n "${POOL_ID:-}" ] && [ "$POOL_ID" != "None" ]; then
  echo "  Login:    $ADMIN_USERNAME  (temp password above)"
fi
echo
echo "  Deploy the scaffolded pipeline:"
echo "      cd pipelines/hello-world && polyris-deploy"
echo "  Then open the Console URL, find hello-world, and click Run."
echo
echo "  Create another pipeline:  polyris-init my-pipeline (run from pipelines/)"
echo "  Tear down everything:     ./scripts/setup-polyris.sh --delete"
