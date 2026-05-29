# claude-tap

Steam Controller (BLE) → tmux keystroke bridge. Tap A on the controller to send `1` to the active pane of any tmux window whose name ends in `-x`. B sends `2`, Y sends `3`. macOS focus is never touched.

See `docs/specs/2026-05-29-claude-tap-design.md` for the design.

## Requirements

- macOS (Apple Silicon or Intel)
- Go 1.21+ for building
- tmux
- An original (2015) Steam Controller with BLE firmware, **bonded to macOS over Bluetooth**

## Setup

```
./bootstrap.sh
```

The script builds the binary, installs a launchd agent, and loads it. macOS will prompt for Bluetooth permission on first run — allow it.

## Operations

**Mark a tmux window as the target:** `C-b ,` then rename it to anything ending in `-x` (e.g., `claude-x`).

**Reload after rebuilding:** re-run `./bootstrap.sh`.

**Stop entirely:** `launchctl unload ~/Library/LaunchAgents/com.hugh.claude-tap.plist`

**Logs:** `~/Library/Logs/claude-tap.log`

## How it works

macOS BLE HID stack doesn't accept feature reports from userspace, so the daemon uses CoreBluetooth (via `tinygo-org/cbgo`). It retrieves the bonded Steam Controller via `RetrieveConnectedPeripheralsWithServices` on Valve's vendor service UUID, writes the disable-lizard command (`0xC0 0x87 0x03 0x08 0x07 0x00`) to characteristic `100f6c34-…`, then subscribes to input notifications on `100f6c33-…`. Button state lives in bytes[3..5] of reports where byte[2]==0x00. A press 0→1 edge triggers `tmux send-keys` to the first window whose name ends in `-x`.

## History

This started as a Python+PyObjC implementation; the rewrite to Go was driven by single-binary distribution. The original Python version is in git history (see commits before `7c0f1ec`).
