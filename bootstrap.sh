#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="agent-pad.plist"
INSTALLED_PLIST="$HOME/Library/LaunchAgents/$PLIST_NAME"
BINARY="$REPO/bin/agent-pad"

echo "==> Building"
mkdir -p "$REPO/bin"
(cd "$REPO" && go build -o "$BINARY" ./cmd/agent-pad)

echo "==> Resolving tmux path"
TMUX_BIN="$(command -v tmux || true)"
if [ -z "$TMUX_BIN" ]; then
  echo "ERROR: tmux not found in PATH. Install tmux first, then re-run bootstrap." >&2
  exit 1
fi
echo "    TMUX_BIN=$TMUX_BIN"

echo "==> Installing launchd plist to $INSTALLED_PLIST"
mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__HOME__|$HOME|g" -e "s|__TMUX_BIN__|$TMUX_BIN|g" \
  "$REPO/launchd/$PLIST_NAME" > "$INSTALLED_PLIST"

echo "==> Loading agent"
launchctl unload "$INSTALLED_PLIST" 2>/dev/null || true
launchctl load "$INSTALLED_PLIST"

echo
echo "Done. Verify with: launchctl list | grep agent-pad"
echo "Logs: ~/Library/Logs/agent-pad.log"
echo
echo "If macOS prompts for Bluetooth permission on first run, allow it in"
echo "System Settings > Privacy & Security > Bluetooth."
