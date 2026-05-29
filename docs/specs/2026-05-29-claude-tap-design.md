# claude-tap: Steam Controller → tmux keystroke bridge

## Problem

Claude Code asks 1/2/3 multiple-choice questions during long tasks. The Claude window typically lives on a side screen, so answering requires changing focus, typing one key, then changing focus back. The cost per question is small but the disruption is high.

## Goal

A way to send a literal `1` or `2` keystroke to the Claude Code window without changing the currently-focused window. Source of the input is a Steam Controller (already paired to the Mac via Bluetooth): A button → `1`, B button → `2`.

Two extra buttons (X, Y) are available if we ever want `3`/`4`; out of scope for v1.

## Non-goals

- Working without a Steam Controller. If the controller is dead/missing/elsewhere, this tool is offline. That's fine.
- Detecting whether Claude is actually waiting for input. The daemon fires keystrokes blindly. If Claude isn't prompting, the digits are appended to the pane's stdin buffer harmlessly.
- Cross-machine sync. Single-laptop tool.

## Architecture

One background process (`claude-tap`) running as a launchd user agent. Two responsibilities:

1. **Read controller.** Open the Steam Controller via HIDAPI, disable lizard mode (so it sends raw button data instead of emulating keyboard/mouse), watch incoming HID reports for A/B button press edges.
2. **Send keystroke.** On a press edge, query tmux for the first window whose `window_name` ends in `-x`, then `tmux send-keys -t <session:window> 1` (A) or `2` (B). tmux routes the keystroke to the active pane in that window without touching macOS focus.

The user marks a window as the target by renaming it with `C-b ,` to something ending in `-x` (e.g., `claude-x`). Self-describing, no external state, survives the daemon restarting.

If no `-x` window exists at the moment of a press, the daemon swallows the press silently.

## Components

### `claude_tap.py` — the daemon

Single Python file. Dependencies: `hid` (Python binding to libhidapi). Targets Python 3.12 (matches the rest of Hugh's tooling).

**Startup:**
1. Loop until `hid.enumerate(0x28de, *)` returns a Steam Controller device. Sleep 1s between attempts.
2. Open the device. Send the "disable lizard mode" feature report — a 64-byte buffer beginning `0x87 0x03 0x08 0x07 0x00`, rest zero-padded.
3. Enter main loop.

**Main loop:**
- `device.read(64, timeout_ms=5000)`.
- Parse bytes 8–11 as a little-endian 32-bit button bitmap. A = bit 23, B = bit 22. (Bit positions cribbed from existing `steamcontroller` Python lib; verify on first run.)
- Maintain `prev_buttons` state. On 0→1 edge for A, fire `1`. For B, fire `2`. Ignore 1→0 edges and held states — one tap = one keystroke.
- On read timeout (no events for 5s), continue. Not an error.
- On read failure (disconnect): break to reconnect loop. Sleep 1s, restart from Startup step 1. Re-send disable-lizard report on reconnect — the controller reverts to lizard mode on wake/reconnect.

**Sending the keystroke:**
- `subprocess.run(["tmux", "list-windows", "-a", "-F", "#{session_name}:#{window_index} #{window_name}"], capture_output=True, text=True, check=False)`.
- Parse output, find first line where `window_name` ends in `-x`.
- If found: `subprocess.run(["tmux", "send-keys", "-t", target, digit], check=False)`.
- If not found: log at debug level and drop.

**Edge cases:**
- tmux not running: `list-windows` exits nonzero, we drop silently.
- Multiple `-x` windows: first match wins. User is expected to maintain at most one.
- Controller asleep (10 min idle): reads stall, then resume. Re-send lizard-disable on next read failure→reconnect.

### launchd integration

`~/Library/LaunchAgents/com.hugh.claude-tap.plist`:
- `RunAtLoad=true`, `KeepAlive=true` (daemon restarts on crash).
- `StandardOutPath` / `StandardErrorPath` → `~/Library/Logs/claude-tap.log`.
- Program path points at `~/src/claude-tap/.venv/bin/python` running `claude_tap.py`.

### Bootstrap

`bootstrap.sh` in repo root:
1. `python3 -m venv .venv`
2. `.venv/bin/pip install hid`
3. Copy plist to `~/Library/LaunchAgents/`, substituting `$HOME`.
4. `launchctl load ~/Library/LaunchAgents/com.hugh.claude-tap.plist`.
5. Print reminder to grant Input Monitoring permission to the Python binary in System Settings → Privacy & Security.

## Operational notes

- **macOS permissions:** First run will fail until Input Monitoring is granted to the venv's Python binary. The plist's `KeepAlive=true` will cause it to busy-loop crashing until permission is granted — fine for first-run, slightly noisy in logs. Acceptable.
- **Verifying buttons during development:** Hex-dump the HID input report and tap A vs B to confirm bit positions. The `steamcontroller` lib's documented offsets are the starting hypothesis, not guaranteed correct for BLE-firmware variants of the controller.
- **Stopping the daemon:** `launchctl unload ~/Library/LaunchAgents/com.hugh.claude-tap.plist`.

## Out of scope (deferred)

- X/Y button mapping for `3`/`4`.
- Tiebreaker logic for multiple `-x` windows (would use `pane_active` or recency).
- Cross-application support beyond tmux.
- Status indicator (e.g., menu bar item showing connection state).
