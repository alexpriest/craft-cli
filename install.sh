#!/bin/bash
# Per-machine setup for the `craft` CLI. The scripts sync via ~/Code, but
# ~/.local/bin (the symlink) and .env (creds) are machine-local — run once per
# machine. Idempotent; safe to re-run.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"
ENV_FILE="$HERE/.env"

echo "==> linking craft + kit-notify into $BIN"
mkdir -p "$BIN"
ln -sf "$HERE/craft" "$BIN/craft"
ln -sf "$HERE/kit-notify" "$BIN/kit-notify"   # the #kit worker calls this to text Alex

# Creds: craft reads .env first, then falls back to `op`. Populate .env so normal
# calls never spawn op (faster, and no stall if 1Password is locked).
have_creds() { grep -qE '^(export )?CRAFT_BASE_URL=.+' "$ENV_FILE" 2>/dev/null \
            && grep -qE '^(export )?CRAFT_CREDENTIAL=.+' "$ENV_FILE" 2>/dev/null; }

if have_creds; then
  echo "==> .env already has creds"
elif command -v op >/dev/null 2>&1 && \
     BASE="$(op read 'op://Claude/Craft API/base_url' 2>/dev/null)" && \
     CRED="$(op read 'op://Claude/Craft API/credential' 2>/dev/null)" && \
     [[ -n "$BASE" && -n "$CRED" ]]; then
  echo "==> writing creds to .env from 1Password"
  { echo "CRAFT_BASE_URL=$BASE"; echo "CRAFT_CREDENTIAL=$CRED"; } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
else
  echo "!! could not read creds from 1Password. Either sign in to \`op\` and re-run,"
  echo "   or create $ENV_FILE with CRAFT_BASE_URL= and CRAFT_CREDENTIAL= by hand"
  echo "   (values: op://Claude/Craft API/{base_url,credential})."
fi

echo "==> smoke test"
if "$BIN/craft" ls --location unsorted >/dev/null 2>&1; then
  echo "done — \`craft\` works"
else
  echo "!! \`craft ls\` failed — check creds above"; exit 1
fi
