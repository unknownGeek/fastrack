#!/usr/bin/env bash
# =====================================================================
# lexor-email - one-click runner (macOS / Linux)
# =====================================================================
# What it does, in order:
#   1. cd into the script's own folder so it works from anywhere.
#   2. Pick a Python 3 interpreter (python3 or python).
#   3. Create .venv on first run.
#   4. Install / upgrade requirements only when requirements.txt changes
#      (tracked via a hash file inside .venv).
#   5. Prompt-create .env from .env.example if the user has not yet
#      configured credentials.
#   6. Sanity-check that config.yaml, the body file, and at least one
#      attachment exist before bothering Gmail.
#   7. Forward any CLI args straight through to send_email.py.
#
# Usage:
#   ./run.sh                 # real send
#   ./run.sh --dry-run       # preview, no SMTP
#   ./run.sh --to a@b.com    # ad-hoc recipient
#   ./run.sh -v --dry-run    # verbose preview
# =====================================================================
set -euo pipefail

# --- locate self -----------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR=".venv"
REQ_FILE="requirements.txt"
REQ_HASH_FILE="$VENV_DIR/.requirements.sha256"
ENV_FILE=".env"
ENV_EXAMPLE=".env.example"
CONFIG_FILE="config.yaml"

# --- pretty logging --------------------------------------------------
c_reset="\033[0m"; c_dim="\033[2m"; c_red="\033[31m"
c_green="\033[32m"; c_yellow="\033[33m"; c_blue="\033[34m"
log()  { printf "${c_blue}[run]${c_reset} %s\n" "$*"; }
ok()   { printf "${c_green}[ok]${c_reset}  %s\n" "$*"; }
warn() { printf "${c_yellow}[warn]${c_reset} %s\n" "$*"; }
die()  { printf "${c_red}[fail]${c_reset} %s\n" "$*" >&2; exit 1; }

# --- pick python -----------------------------------------------------
PY=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
            PY="$cand"; break
        fi
    fi
done
[[ -n "$PY" ]] || die "Python 3.8+ is required but was not found on PATH."
log "using $($PY -V 2>&1) at $(command -v "$PY")"

# --- venv ------------------------------------------------------------
if [[ ! -d "$VENV_DIR" ]]; then
    log "creating virtualenv in $VENV_DIR ..."
    "$PY" -m venv "$VENV_DIR" || die "failed to create venv. Try: $PY -m pip install --user virtualenv"
    ok "venv created"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
VENV_PY="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

# --- dependency sync (only when requirements.txt changes) ------------
if command -v shasum >/dev/null 2>&1; then
    HASH_CMD="shasum -a 256"
elif command -v sha256sum >/dev/null 2>&1; then
    HASH_CMD="sha256sum"
else
    HASH_CMD=""
fi

current_hash=""
if [[ -n "$HASH_CMD" && -f "$REQ_FILE" ]]; then
    current_hash="$($HASH_CMD "$REQ_FILE" | awk '{print $1}')"
fi
saved_hash=""
[[ -f "$REQ_HASH_FILE" ]] && saved_hash="$(cat "$REQ_HASH_FILE")"

if [[ -z "$current_hash" || "$current_hash" != "$saved_hash" ]]; then
    log "installing dependencies from $REQ_FILE ..."
    "$VENV_PIP" install --upgrade pip --quiet
    "$VENV_PIP" install -r "$REQ_FILE" --quiet
    [[ -n "$current_hash" ]] && echo "$current_hash" > "$REQ_HASH_FILE"
    ok "dependencies ready"
else
    log "dependencies up-to-date (cache hit)"
fi

# --- preflight: .env -------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
    if [[ -f "$ENV_EXAMPLE" ]]; then
        warn "$ENV_FILE not found."
        printf "      Create one from $ENV_EXAMPLE now? [Y/n] "
        # read from terminal even if stdin is piped
        if [[ -t 0 ]]; then read -r reply; else reply="n"; fi
        reply="${reply:-Y}"
        if [[ "$reply" =~ ^[Yy]$ ]]; then
            cp "$ENV_EXAMPLE" "$ENV_FILE"
            ok "$ENV_FILE created. Open it and set GMAIL_SENDER_EMAIL + GMAIL_APP_PASSWORD,"
            ok "then re-run this script."
            exit 0
        else
            warn "skipping .env creation. Make sure credentials are in config.yaml or env vars."
        fi
    else
        warn "$ENV_FILE not found and no $ENV_EXAMPLE template available."
    fi
fi

# --- preflight: config + body + attachments --------------------------
[[ -f "$CONFIG_FILE" ]] || die "$CONFIG_FILE missing. See README.md."

body_path="$($VENV_PY - <<'PY'
import sys, yaml, pathlib
cfg = yaml.safe_load(open("config.yaml")) or {}
print((cfg.get("email") or {}).get("body_markdown_path", "email_body.md"))
PY
)"
[[ -f "$body_path" ]] || die "body markdown not found at '$body_path' (set in config.yaml)."

attach_check="$($VENV_PY - <<'PY'
import yaml, pathlib, sys
cfg = yaml.safe_load(open("config.yaml")) or {}
items = (cfg.get("email") or {}).get("attachments") or []
if isinstance(items, str): items = [items]
missing = [str(p) for p in items if not pathlib.Path(p).is_file()]
if missing:
    print("MISSING:" + "|".join(missing))
else:
    print(f"OK:{len(items)}")
PY
)"
case "$attach_check" in
    MISSING:*)
        die "configured attachment(s) not found: ${attach_check#MISSING:}. Drop the file(s) into ./attachments/ or update config.yaml."
        ;;
    OK:0)
        warn "no attachments configured in config.yaml (sending body only)."
        ;;
    OK:*)
        ok "attachments present (${attach_check#OK:})."
        ;;
esac

# --- run -------------------------------------------------------------
log "launching send_email.py $*"
echo "----------------------------------------------------------------------"
exec "$VENV_PY" send_email.py "$@"
