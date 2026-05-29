# claude-tap

Steam Controller (BLE) → tmux keystroke bridge. Tap A on the controller to send `1` to the active pane of any tmux window whose name ends in `-x`. B sends `2`, Y sends `3`. macOS focus is never touched.

See `docs/specs/2026-05-29-claude-tap-design.md` for the design and `docs/plans/2026-05-29-claude-tap.md` for the (now somewhat stale) implementation plan.

## Setup

```
./bootstrap.sh
```

The Steam Controller must already be bonded to macOS over Bluetooth. macOS may prompt for Bluetooth access on first run — allow it.

## Operations

**Mark a tmux window as the target:** `C-b ,` then rename it to anything ending in `-x` (e.g., `claude-x`).

**Reload after code changes:**
```
launchctl unload ~/Library/LaunchAgents/com.hugh.claude-tap.plist
launchctl load ~/Library/LaunchAgents/com.hugh.claude-tap.plist
```

**Stop entirely:** `launchctl unload ~/Library/LaunchAgents/com.hugh.claude-tap.plist`

**Logs:** `~/Library/Logs/claude-tap.log`

## How it works

Brief: macOS BLE HID stack can't accept feature reports through HIDAPI, so the daemon uses CoreBluetooth (PyObjC). It retrieves the bonded Steam Controller via `retrieveConnectedPeripheralsWithServices` on Valve's vendor service UUID, writes the disable-lizard command (`0xC0 0x87 0x03 0x08 0x07 0x00`) to characteristic `100f6c34-…`, then subscribes to input notifications on `100f6c33-…`. Button state lives in bytes[3..5] of reports where byte[2]==0x00. A press 0→1 edge triggers `tmux send-keys` to the first window whose name ends in `-x`.
