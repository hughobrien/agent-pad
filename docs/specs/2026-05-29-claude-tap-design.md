# claude-tap: Steam Controller → tmux keystroke bridge

> Updated 2026-05-29 to match the as-built implementation. The original spec assumed HIDAPI; the macOS BLE HID stack forced a CoreBluetooth-based design instead. The history of how we got here is preserved in commits and the implementation plan.

## Problem

Claude Code asks 1/2/3 multiple-choice questions during long tasks. The Claude window typically lives on a side screen, so answering requires changing focus, typing one key, then changing focus back. The cost per question is small but the disruption is high.

## Goal

A way to send a literal `1`, `2`, or `3` keystroke to the Claude Code window without changing the currently-focused window. Source of the input is a Steam Controller (the original 2015 model, paired to the Mac over Bluetooth Low Energy): A → `1`, B → `2`, Y → `3`. Extending to more buttons is a one-line change in `BUTTON_TO_DIGIT`.

## Non-goals

- Working without a Steam Controller. If it's dead, missing, or unbonded, this tool is offline. Fine.
- Detecting whether Claude is actually waiting for input. The daemon fires keystrokes blindly; if Claude isn't prompting, the digits land harmlessly in stdin.
- Cross-machine sync. Single-laptop tool.

## Why CoreBluetooth, not HIDAPI

On macOS, a BLE-paired Steam Controller is owned by the kernel's `IOBluetoothHIDDriver`. HIDAPI on macOS sees its HID interfaces but cannot send feature reports — every `send_feature_report` returns -1 silently. The disable-lizard-mode command (the one Linux uses) goes nowhere. There is no userspace-accessible HID feature-report channel on macOS for BLE-paired HID devices.

The workaround: bypass HID entirely and talk to the Steam Controller's **vendor-defined GATT service** through CoreBluetooth. CBCentralManager's `retrieveConnectedPeripheralsWithServices` returns the controller when queried by the vendor service UUID, *even while it is bonded as HID*. We get parallel GATT access: the kernel handles HID input, we own the vendor channel for control and our own input notifications.

## Steam Controller GATT details

- **Vendor service:** `100f6c32-1735-4313-b402-38567131e5f3`
- **Input characteristic (notify):** `100f6c33-1735-4313-b402-38567131e5f3` — emits 19-byte reports. When byte[2] == `0x00`, bytes[3..5] hold the standard 24-bit Steam Controller button bitmap (per the canonical decoding at https://dennis-hamester.gitlab.io/scraw/protocol/). Byte[3] is the high byte; byte[5] is the low byte.
- **Output characteristic (write):** `100f6c34-1735-4313-b402-38567131e5f3` — accepts short framed commands. A single-segment command wraps the payload in a 2-byte BLE framing header: `0xC0` (data flag | last flag | segment 0) + opcode + opcode bytes. Writes with response are length-limited to ~20 bytes (BLE MTU).

### Disable-lizard command

Six bytes, written with response:

```
0xC0 0x87 0x03 0x08 0x07 0x00
 │    │    │    │    │    │
 │    │    │    │    │    └─ pad
 │    │    │    │    └────── trackpad mode value
 │    │    │    └─────────── register: LEFT_TRACKPAD_MODE
 │    │    └──────────────── number of bytes following
 │    └───────────────────── SET_SETTINGS opcode
 └────────────────────────── BLE single-segment framing header
```

The disable persists for the lifetime of the BLE connection — no heartbeat needed. (Verified empirically: 60s idle with no resend, lizard mode stayed off.) On reconnect after a disconnect, the daemon's `peripheral_didDiscoverCharacteristicsForService_error_` callback fires again and re-sends.

### Button bitmap

Combine bytes[3..5] of a button report into a 24-bit value (big-endian: byte[3] is the high byte). Bit masks for the original controller:

| Mask | Button |
|------|--------|
| `0x000001` | Right grip |
| `0x000002` | Left trackpad / stick click |
| `0x000004` | Right trackpad press |
| `0x000008` | Left trackpad touch |
| `0x000010` | Right trackpad touch |
| `0x000040` | Stick |
| `0x001000` | Select (Back) |
| `0x002000` | Steam |
| `0x004000` | Start (Forward) |
| `0x008000` | Left grip |
| `0x010000` | Right trigger |
| `0x020000` | Left trigger |
| `0x040000` | Right shoulder |
| `0x080000` | Left shoulder |
| `0x100000` | Y |
| `0x200000` | B |
| `0x400000` | X |
| `0x800000` | A |

Edge-triggered on 0→1 transitions per bit; holding does not repeat.

## Architecture

One Python process running as a launchd user agent. Built on `pyobjc-framework-CoreBluetooth`. Single file (`claude_tap.py`), ~180 lines.

A CBCentralManager owns one delegate object that implements both the CBCentralManager and CBPeripheral delegate protocols. The flow:

1. **Power-on:** `centralManagerDidUpdateState_` fires once when Bluetooth is ready, calls `_try_attach`.
2. **Attach:** `_try_attach` runs `retrieveConnectedPeripheralsWithServices` filtered by the vendor service UUID. If empty (controller asleep or unbonded), logs and returns — the main loop will poll again in 2 seconds. If populated, calls `connectPeripheral`.
3. **Connect:** `centralManager_didConnectPeripheral_` discovers the vendor service.
4. **Discover:** `peripheral_didDiscoverCharacteristicsForService_error_` resolves the input and output characteristics, writes the disable-lizard command to output, and calls `setNotifyValue_forCharacteristic_(True)` on input. Sets `self.attached = True`.
5. **Input loop:** `peripheral_didUpdateValueForCharacteristic_error_` fires per notification. Button reports (byte[2] == 0x00) get parsed into a 24-bit bitmap; press edges trigger `send_keystroke`.
6. **Disconnect:** `centralManager_didDisconnectPeripheral_` clears `self.attached`. The main loop's poll then re-attempts attach every 2 seconds until the controller comes back.
7. **Bluetooth off:** `centralManagerDidUpdateState_` fires with a non-`5` state. Drop the peripheral; do nothing further until state returns to `5`.

The main loop is a `runUntilDate_` call in a Python `while True:` — it pumps the CoreFoundation runloop in 50 ms increments and handles the periodic poll for re-attach.

## tmux integration

`resolve_tmux_target` runs `tmux list-windows -a -F "#{session_name}:#{window_index} #{window_name}"` and returns the first target whose `window_name` ends in `-x`. To mark a window, use tmux's native rename (`C-b ,`). If no `-x` window exists, the daemon drops the press silently.

`send_keystroke` runs `tmux send-keys -t <target> <digit>`. tmux routes the keystroke to the active pane of that window without touching macOS focus.

### tmux path resolution

`launchd`-spawned processes inherit a stripped PATH (`/usr/bin:/bin:/usr/sbin:/sbin`). Hugh's tmux is installed via Nix at `/nix/store/<hash>-tmux-<version>/bin/tmux`, which is not on launchd's PATH. Sourcing `.zshrc` from a child shell at daemon startup turned out to be unreliable (empty PATH propagation under `env -i`-style invocation).

Solution: resolve the tmux path once at install time (in `bootstrap.sh`, which runs from the user's interactive shell) and bake the result into the plist's `EnvironmentVariables.TMUX_BIN`. The daemon reads `$TMUX_BIN` at startup, falls back to `shutil.which("tmux")` for non-launchd invocations. If Hugh upgrades tmux via Nix (changing the store hash), re-running `bootstrap.sh` regenerates the plist.

## Setup constraints

- The Steam Controller must be **bonded to macOS over BLE** before the daemon runs. The daemon does not initiate pairing; it parallel-accesses an already-bonded peripheral. If unbonded, macOS will not surface it via `retrieveConnectedPeripheralsWithServices`.
- macOS will prompt for Bluetooth access on first daemon run. Grant it (System Settings → Privacy & Security → Bluetooth).
- The controller must be the original 2015 Steam Controller with BLE firmware (VID `0x28DE`, PID `0x1106`). The newer SC2026 uses a different protocol and characteristic layout; this design does not address it.

## Operational notes

- **Logs:** `~/Library/Logs/claude-tap.log`.
- **Restart:** `launchctl unload ~/Library/LaunchAgents/com.hugh.claude-tap.plist && launchctl load …` (or re-run `bootstrap.sh`).
- **Heartbeat:** none. Disable-lizard persists until the BLE link drops.
- **Reconnect cost:** ~3-5 seconds after a Bluetooth toggle, dominated by macOS re-bonding the controller. The daemon polls every 2 seconds and resumes once macOS reports the peripheral as connected.

## Out of scope

- Pairing flow for an unbonded controller (would require dropping HID ownership, racing macOS for first connect, GATT bonding — explored and rejected during design).
- Steam Controller 2026 (different protocol).
- Cross-application: only tmux is supported as a destination.
- Status indicator (menu bar item, etc.).
