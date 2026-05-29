#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.hugh.claude-tap.plist"
INSTALLED_PLIST="$HOME/Library/LaunchAgents/$PLIST_NAME"

echo "==> Creating venv"
[ -d "$REPO/.venv" ] || python3 -m venv "$REPO/.venv"

echo "==> Installing dependencies"
"$REPO/.venv/bin/pip" install -q -r "$REPO/requirements.txt"

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
echo "Done. Verify with: launchctl list | grep claude-tap"
echo "Logs: ~/Library/Logs/claude-tap.log"
echo
echo "If it can't access Bluetooth, macOS will prompt for permission on first run."
echo "Grant it in System Settings > Privacy & Security > Bluetooth."
