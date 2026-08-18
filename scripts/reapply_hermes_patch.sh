#!/usr/bin/env bash
# Re-apply the Telegram TypeHandler lazy-install fix after `hermes update`.
#
# `hermes update` auto-stashes uncommitted changes in the hermes-agent checkout,
# which silently reverts this fix and breaks the Telegram gateway with
# "Any cannot be instantiated" on the next restart.
#
# Run this after every `hermes update`. Idempotent — exits cleanly if the fix
# is already present.
set -euo pipefail

HERMES_DIR="$HOME/.hermes/hermes-agent"
ADAPTER="$HERMES_DIR/plugins/platforms/telegram/adapter.py"
PATCH="$HOME/atma-app/patches/hermes_typehandler_fix.patch"

if [[ ! -f "$ADAPTER" ]]; then
  echo "ERROR: hermes-agent adapter not found at $ADAPTER" >&2
  exit 1
fi

if grep -q "global CommandHandler, CallbackQueryHandler, TypeHandler, TelegramMessageHandler" "$ADAPTER" 2>/dev/null; then
  echo "TypeHandler fix already present — nothing to do."
  exit 0
fi

if [[ ! -f "$PATCH" ]]; then
  echo "ERROR: patch file not found at $PATCH" >&2
  exit 1
fi

cd "$HERMES_DIR"
if git apply --check "$PATCH" 2>/dev/null; then
  git apply "$PATCH"
  echo "TypeHandler fix re-applied."
  echo "Restart the gyani gateway to pick it up:"
  echo "  systemctl --user restart hermes-gateway-gyani.service"
else
  echo "WARNING: patch does not apply cleanly — the upstream code may have changed." >&2
  echo "Re-apply manually per $PATCH" >&2
  exit 1
fi
