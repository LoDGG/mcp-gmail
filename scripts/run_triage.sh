#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$repo_root"
mkdir -p .local
chmod 700 .local
log_file="$repo_root/.local/triage.log"
lock_file="$repo_root/.local/triage.lock"
touch "$log_file"
chmod 600 "$log_file"

# Acquire the lock before loading credentials or making any API request.
exec 9>"$lock_file"
if ! flock -n 9; then
    printf '%s skipped: another triage run is active\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >> "$log_file"
    exit 0
fi

exec >> "$log_file" 2>&1
printf '%s start\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
finish() {
    status=$?
    printf '%s end status=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$status"
}
trap finish EXIT

secret_env="$repo_root/.secrets/gemini.env"
if [[ ! -r "$secret_env" ]]; then
    printf 'Missing readable .secrets/gemini.env\n' >&2
    exit 1
fi
# Read the one expected assignment as data, without executing shell code.
gemini_key=""
while IFS= read -r line || [[ -n "${line:-}" ]]; do
    case "$line" in
        GEMINI_API_KEY=*) gemini_key="${line#GEMINI_API_KEY=}" ;;
    esac
done < "$secret_env"
if [[ -z "$gemini_key" ]]; then
    printf 'GEMINI_API_KEY is missing from .secrets/gemini.env\n' >&2
    exit 1
fi
export GEMINI_API_KEY="$gemini_key"
unset gemini_key line

if [[ -n "${HOME:-}" ]]; then
    export PATH="$HOME/.local/bin:$PATH"
fi
export GMAIL_BACKEND=real
export GMAIL_LABEL_WRITES=1

uv run python -m gmail_agent.triage_cli \
    --query 'in:inbox -label:"Processed" newer_than:14d' \
    --max-emails 10 \
    --batch-size 10 \
    --apply
