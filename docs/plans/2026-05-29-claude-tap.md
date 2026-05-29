# claude-tap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a launchd-managed daemon that listens for A/B button presses on a Bluetooth-paired Steam Controller and sends `1`/`2` keystrokes to a tmux window marked with an `-x` suffix in its name, without touching macOS window focus.

**Architecture:** Single-file Python daemon. Talks to the Steam Controller via HIDAPI, parses raw button bitmaps from the 64-byte input reports, shells out to `tmux send-keys` on press edges. Self-restarting via launchd `KeepAlive`. No tests beyond a unit test for the button-bitmap parser — everything else is hardware-dependent and verified by tapping the controller.

**Tech Stack:** Python 3.12, `hid` (libhidapi binding), tmux, launchd.

---

## File Structure

```
~/src/claude-tap/
├── .gitignore
├── README.md
├── requirements.txt
├── bootstrap.sh
├── claude_tap.py                       # the daemon
├── tests/
│   └── test_button_parser.py           # unit test for parse_buttons
├── launchd/
│   └── com.hugh.claude-tap.plist       # launchd template
└── docs/
    ├── specs/2026-05-29-claude-tap-design.md
    └── plans/2026-05-29-claude-tap.md
```

`claude_tap.py` is kept as one file because the components (HID I/O, parsing, tmux subprocess) are tightly coupled to the daemon loop's lifecycle. Splitting them adds indirection without making any piece independently testable in a useful way.

---

## Task 0: Project skeleton

**Goal:** Repo has venv, dependency manifest, gitignore, and a runnable stub.

**Files:**
- Create: `requirements.txt`
- Create: `.gitignore`
- Create: `claude_tap.py` (stub)
- Create: `README.md`

**Acceptance Criteria:**
- [ ] `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` succeeds
- [ ] `.venv/bin/python claude_tap.py` runs and prints "claude-tap starting" then exits cleanly
- [ ] `.venv/` is gitignored

**Verify:** `.venv/bin/python claude_tap.py` → prints `claude-tap starting` and exits 0.

**Steps:**

- [ ] **Step 1: Write `.gitignore`**

```
.venv/
__pycache__/
*.pyc
.pytest_cache/
*.log
```

- [ ] **Step 2: Write `requirements.txt`**

```
hid==1.3.1
pytest==8.3.3
```

(Pinned versions are last-known-good as of writing; if `hid` fails to install on Apple Silicon, try `hidapi==0.14.0` instead — different package, same purpose.)

- [ ] **Step 3: Write `claude_tap.py` stub**

```python
"""claude-tap: Steam Controller -> tmux keystroke bridge."""
from __future__ import annotations

import logging
import sys

log = logging.getLogger("claude-tap")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log.info("claude-tap starting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Write `README.md`**

```markdown
# claude-tap

Steam Controller -> tmux keystroke bridge. Tap A on the controller to send `1` to the active pane of any tmux window whose name ends in `-x`. Tap B to send `2`.

See `docs/specs/2026-05-29-claude-tap-design.md` for the design.

## Setup

```
./bootstrap.sh
```

Then grant Input Monitoring permission to `.venv/bin/python` in System Settings -> Privacy & Security.
```

- [ ] **Step 5: Create venv and install**

Run: `cd ~/src/claude-tap && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
Expected: pip output ends with `Successfully installed hid-... pytest-...`

- [ ] **Step 6: Smoke-test the stub**

Run: `.venv/bin/python claude_tap.py`
Expected: one line of log output containing `claude-tap starting`, exit 0.

- [ ] **Step 7: Commit**

```bash
git -C ~/src/claude-tap add .gitignore requirements.txt claude_tap.py README.md
git -C ~/src/claude-tap commit -m "Project skeleton"
```

---

## Task 1: HID discovery + lizard mode disable

**Goal:** Daemon finds the Steam Controller via HIDAPI, opens it, disables lizard mode, and prints raw input reports to stdout for manual inspection. This is the foundation — without this working, nothing else does.

**Files:**
- Modify: `claude_tap.py`

**Acceptance Criteria:**
- [ ] Daemon enumerates HID devices and finds a Steam Controller (vendor `0x28de`)
- [ ] Daemon opens the device successfully (with Input Monitoring permission granted)
- [ ] Daemon sends the disable-lizard-mode feature report
- [ ] Daemon prints raw 64-byte input reports as hex when buttons are pressed
- [ ] Pressing A produces a visibly different hex dump than pressing B

**Verify:** Run the daemon. Tap A and B. Confirm hex output changes visibly between the two.

**Steps:**

- [ ] **Step 1: Add device discovery**

Replace the body of `main()` with:

```python
import hid

STEAM_VID = 0x28DE
DISABLE_LIZARD = bytes([0x87, 0x03, 0x08, 0x07, 0x00] + [0x00] * 59)


def find_controller() -> dict | None:
    for dev in hid.enumerate(STEAM_VID, 0):
        log.info("Found device: vid=%04x pid=%04x usage=%s path=%s",
                 dev["vendor_id"], dev["product_id"],
                 dev.get("usage"), dev.get("path"))
        return dev
    return None


def open_controller():
    info = find_controller()
    if info is None:
        raise RuntimeError("No Steam Controller found. Is it powered on and paired?")
    device = hid.device()
    device.open_path(info["path"])
    log.info("Opened Steam Controller")
    device.send_feature_report(DISABLE_LIZARD)
    log.info("Sent disable-lizard-mode report")
    return device
```

- [ ] **Step 2: Add raw-report dump loop**

```python
def dump_reports(device) -> None:
    log.info("Reading reports. Tap buttons to see output. Ctrl-C to stop.")
    while True:
        data = device.read(64, timeout_ms=5000)
        if not data:
            continue
        # Only print frames where something changed from the resting state
        # (Steam Controller spams reports at ~125Hz; resting bytes 8-11 are 0)
        if any(data[8:12]):
            log.info("report: %s", bytes(data).hex())
```

- [ ] **Step 3: Wire up `main`**

```python
def main() -> int:
    logging.basicConfig(level=logging.INFO,
                       format="%(asctime)s %(levelname)s %(message)s")
    log.info("claude-tap starting")
    device = open_controller()
    try:
        dump_reports(device)
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        device.close()
    return 0
```

- [ ] **Step 4: Grant Input Monitoring permission**

Run: `.venv/bin/python claude_tap.py`
Expected: First run will likely fail with a permission error or open silently with no reports. Open System Settings -> Privacy & Security -> Input Monitoring, click +, and add `~/src/claude-tap/.venv/bin/python3.12` (the actual binary, not the symlink).

If the file picker won't accept the symlink path, navigate to it with `Cmd+Shift+G`.

- [ ] **Step 5: Verify with both buttons**

Run: `.venv/bin/python claude_tap.py`
Tap A. Tap B. Observe hex output changes between the two.
Expected: Bytes 8–11 differ between A presses and B presses. Note which bits flip for each — this informs Task 2.

- [ ] **Step 6: Commit**

```bash
git -C ~/src/claude-tap add claude_tap.py
git -C ~/src/claude-tap commit -m "Discover Steam Controller and disable lizard mode"
```

---

## Task 2: Button bitmap parser (unit-tested)

**Goal:** Pure function that takes a 64-byte HID report and returns a set of pressed button names. Unit-tested with canned bytes captured in Task 1.

**Files:**
- Modify: `claude_tap.py`
- Create: `tests/__init__.py`
- Create: `tests/test_button_parser.py`

**Acceptance Criteria:**
- [ ] `parse_buttons(report_bytes)` returns `{"A"}` when only A is held
- [ ] `parse_buttons(report_bytes)` returns `{"B"}` when only B is held
- [ ] `parse_buttons(report_bytes)` returns `set()` when no buttons are held
- [ ] `pytest tests/` passes

**Verify:** `cd ~/src/claude-tap && .venv/bin/pytest tests/ -v` → all tests pass.

**Steps:**

- [ ] **Step 1: Capture real report bytes from Task 1**

Run the dumper from Task 1 again. Hold A — copy the hex string into a scratch note. Release. Hold B — copy. Release — capture a "resting" report. You need three: A-held, B-held, idle.

If your captured bytes show A at a different bit position than the assumption below, adjust both `parse_buttons` and the test fixtures accordingly. The constants below match the standard Steam Controller report format from `steamcontroller` (Stany Marcel's lib).

- [ ] **Step 2: Write the failing test**

Create `tests/__init__.py` (empty file).

Create `tests/test_button_parser.py`:

```python
"""Unit tests for the button bitmap parser."""
from claude_tap import parse_buttons


# Captured fixtures — replace bytes 8-11 with your actual captures from Task 1
# These match the standard Steam Controller report layout.
IDLE = bytes.fromhex("01000000" * 16)  # 64 zero-ish bytes
A_HELD = bytearray(IDLE)
A_HELD[8:12] = (0x00800000).to_bytes(4, "little")
A_HELD = bytes(A_HELD)
B_HELD = bytearray(IDLE)
B_HELD[8:12] = (0x00400000).to_bytes(4, "little")
B_HELD = bytes(B_HELD)


def test_idle_report_has_no_buttons():
    assert parse_buttons(IDLE) == set()


def test_a_button_held():
    assert parse_buttons(A_HELD) == {"A"}


def test_b_button_held():
    assert parse_buttons(B_HELD) == {"B"}


def test_short_report_returns_empty():
    assert parse_buttons(b"\x00" * 4) == set()
```

- [ ] **Step 3: Run test, verify it fails**

Run: `.venv/bin/pytest tests/ -v`
Expected: FAIL with `ImportError: cannot import name 'parse_buttons'`.

- [ ] **Step 4: Add the parser**

Add to `claude_tap.py` near the top, after the constants:

```python
BUTTON_BITS = {
    "A": 1 << 23,
    "B": 1 << 22,
    # X is bit 21, Y is bit 20 — added if we ever want 3/4
}


def parse_buttons(report: bytes) -> set[str]:
    """Return the set of button names currently held in this HID report."""
    if len(report) < 12:
        return set()
    bitmap = int.from_bytes(report[8:12], "little")
    return {name for name, mask in BUTTON_BITS.items() if bitmap & mask}
```

- [ ] **Step 5: Run test, verify pass**

Run: `.venv/bin/pytest tests/ -v`
Expected: 4 passed.

If a test fails because the bit position is wrong: look at your captured A_HELD bytes from Task 1, find which bit flipped vs idle, and update `BUTTON_BITS["A"]`. Same for B.

- [ ] **Step 6: Commit**

```bash
git -C ~/src/claude-tap add claude_tap.py tests/
git -C ~/src/claude-tap commit -m "Add button bitmap parser with tests"
```

---

## Task 3: Edge detection + tmux send-keys

**Goal:** Daemon detects press edges (transitions from not-held to held), looks up the target tmux window by `-x` suffix, and sends the appropriate digit. End-to-end working with a real Claude session.

**Files:**
- Modify: `claude_tap.py`

**Acceptance Criteria:**
- [ ] Rename a tmux window to something ending in `-x`. Tap A on the controller. The digit `1` appears in the active pane of that window.
- [ ] Tap B. Digit `2` appears.
- [ ] Holding A for two seconds still only sends one `1` (edge-triggered, not repeating).
- [ ] If no `-x` window exists, taps are silently dropped (log at debug level only).

**Verify:** Manual end-to-end test. In one tmux window, rename to `test-x` and run `cat`. Tap A on the controller. `1` should appear in the cat. Same for B → `2`.

**Steps:**

- [ ] **Step 1: Replace `dump_reports` with the real loop**

Remove `dump_reports` from `claude_tap.py`. Add:

```python
import subprocess


BUTTON_TO_DIGIT = {"A": "1", "B": "2"}


def resolve_target() -> str | None:
    """Return tmux target spec for the first window with name ending in -x."""
    result = subprocess.run(
        ["tmux", "list-windows", "-a",
         "-F", "#{session_name}:#{window_index} #{window_name}"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        target, _, name = line.partition(" ")
        if name.endswith("-x"):
            return target
    return None


def send_keystroke(digit: str) -> None:
    target = resolve_target()
    if target is None:
        log.debug("No -x window; dropping %s", digit)
        return
    log.info("sending %s -> %s", digit, target)
    subprocess.run(["tmux", "send-keys", "-t", target, digit], check=False)


def event_loop(device) -> None:
    prev: set[str] = set()
    while True:
        data = device.read(64, timeout_ms=5000)
        if not data:
            continue
        current = parse_buttons(bytes(data))
        for button in current - prev:
            digit = BUTTON_TO_DIGIT.get(button)
            if digit:
                send_keystroke(digit)
        prev = current
```

- [ ] **Step 2: Update `main` to call `event_loop`**

Replace the `dump_reports(device)` call in `main` with `event_loop(device)`.

- [ ] **Step 3: Manual test setup**

In a tmux session, rename a window: `C-b ,` then type `test-x` and Enter.
In that window's active pane, run: `cat`

- [ ] **Step 4: Run daemon and tap buttons**

In another terminal: `.venv/bin/python ~/src/claude-tap/claude_tap.py`
Tap A on the controller. Look at the `cat` window — `1` should appear.
Tap B — `2` should appear.
Hold A for 2 seconds — only ONE `1` should appear.
Rename the window away from `-x` (`C-b ,` → `nope`). Tap A. Nothing should happen.

- [ ] **Step 5: Commit**

```bash
git -C ~/src/claude-tap add claude_tap.py
git -C ~/src/claude-tap commit -m "Wire button edges to tmux send-keys"
```

---

## Task 4: Reconnect on disconnect/sleep

**Goal:** Daemon survives the controller going to sleep (10 min idle), the laptop going to sleep, or Bluetooth being toggled off and on. After any of these, the next button press should still work.

**Files:**
- Modify: `claude_tap.py`

**Acceptance Criteria:**
- [ ] Toggle Bluetooth off, wait 5 seconds, toggle on. Wait for the controller to reconnect (LED indicates pairing). Tap A → `1` appears in the `-x` window.
- [ ] Leave the controller idle for ~10 min so it sleeps. Wake it with a button press. The first press may be lost (waking the controller) but a follow-up press works.
- [ ] Daemon logs each reconnect attempt at INFO level.

**Verify:** Toggle Bluetooth off and back on. Watch daemon logs. Tap A — `1` appears.

**Steps:**

- [ ] **Step 1: Wrap startup in a reconnect loop**

Replace `main` with:

```python
import time

RECONNECT_DELAY_S = 1.0


def run_forever() -> None:
    while True:
        try:
            device = open_controller()
        except (RuntimeError, OSError) as exc:
            log.info("controller unavailable (%s); retrying in %ss",
                     exc, RECONNECT_DELAY_S)
            time.sleep(RECONNECT_DELAY_S)
            continue
        try:
            event_loop(device)
        except (OSError, IOError) as exc:
            log.info("read failure (%s); reconnecting", exc)
        finally:
            try:
                device.close()
            except Exception:
                pass


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                       format="%(asctime)s %(levelname)s %(message)s")
    log.info("claude-tap starting")
    try:
        run_forever()
    except KeyboardInterrupt:
        log.info("stopping")
    return 0
```

The `open_controller` call already re-sends the disable-lizard report each time, so post-reconnect we're back in controller mode automatically.

- [ ] **Step 2: Test disconnect handling**

Run: `.venv/bin/python ~/src/claude-tap/claude_tap.py`
Toggle Mac's Bluetooth off in the menu bar.
Expected log: `controller unavailable (...); retrying in 1.0s` repeating every second.
Toggle Bluetooth back on. Wait for the controller LED to confirm pairing (a few seconds).
Expected log: `Opened Steam Controller` and `Sent disable-lizard-mode report`.
Tap A → `1` appears in your `-x` window.

- [ ] **Step 3: Commit**

```bash
git -C ~/src/claude-tap add claude_tap.py
git -C ~/src/claude-tap commit -m "Auto-reconnect on Bluetooth/sleep disconnects"
```

---

## Task 5: launchd integration + bootstrap

**Goal:** Daemon runs automatically at login, restarts on crash, logs to a known file.

**Files:**
- Create: `launchd/com.hugh.claude-tap.plist`
- Create: `bootstrap.sh`
- Modify: `README.md` (add operational notes)

**Acceptance Criteria:**
- [ ] `./bootstrap.sh` from a fresh repo state sets up venv, installs deps, installs plist, and loads via launchctl.
- [ ] `launchctl list | grep claude-tap` shows the agent loaded.
- [ ] After reboot, tapping A in an `-x` window sends `1` without manually starting anything.
- [ ] Logs land in `~/Library/Logs/claude-tap.log`.

**Verify:** `launchctl unload`, then `./bootstrap.sh`, then `launchctl list | grep claude-tap` should show the loaded agent. Tap A → `1` appears.

**Steps:**

- [ ] **Step 1: Write the plist template**

Create `launchd/com.hugh.claude-tap.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.hugh.claude-tap</string>

  <key>ProgramArguments</key>
  <array>
    <string>__HOME__/src/claude-tap/.venv/bin/python</string>
    <string>__HOME__/src/claude-tap/claude_tap.py</string>
  </array>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>ThrottleInterval</key>
  <integer>5</integer>

  <key>StandardOutPath</key>
  <string>__HOME__/Library/Logs/claude-tap.log</string>

  <key>StandardErrorPath</key>
  <string>__HOME__/Library/Logs/claude-tap.log</string>
</dict>
</plist>
```

`ThrottleInterval=5` prevents a tight crash loop if (e.g.) Input Monitoring permission is revoked.

- [ ] **Step 2: Write `bootstrap.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.hugh.claude-tap.plist"
INSTALLED_PLIST="$HOME/Library/LaunchAgents/$PLIST_NAME"

echo "==> Creating venv"
[ -d "$REPO/.venv" ] || python3 -m venv "$REPO/.venv"

echo "==> Installing dependencies"
"$REPO/.venv/bin/pip" install -q -r "$REPO/requirements.txt"

echo "==> Installing launchd plist to $INSTALLED_PLIST"
mkdir -p "$HOME/Library/LaunchAgents"
sed "s|__HOME__|$HOME|g" "$REPO/launchd/$PLIST_NAME" > "$INSTALLED_PLIST"

echo "==> Loading agent"
launchctl unload "$INSTALLED_PLIST" 2>/dev/null || true
launchctl load "$INSTALLED_PLIST"

echo
echo "==> Done. Verify with: launchctl list | grep claude-tap"
echo "==> Logs: ~/Library/Logs/claude-tap.log"
echo
echo "If this is a fresh install, grant Input Monitoring permission to:"
echo "  $REPO/.venv/bin/python3.12"
echo "in System Settings -> Privacy & Security -> Input Monitoring."
```

Then: `chmod +x bootstrap.sh`

- [ ] **Step 3: Run bootstrap**

Run: `cd ~/src/claude-tap && ./bootstrap.sh`
Expected: prints each step, finishes without error.

Run: `launchctl list | grep claude-tap`
Expected: a line showing the agent with PID and exit code 0.

- [ ] **Step 4: Verify it's actually running**

Run: `tail -f ~/Library/Logs/claude-tap.log`
Expected: lines showing `claude-tap starting` and `Opened Steam Controller`.

In a tmux window renamed to `*-x`, run `cat`. Tap A on the controller. `1` should appear.

- [ ] **Step 5: Update README with operational notes**

Append to `README.md`:

```markdown
## Operations

**Reload after code changes:**
```
launchctl unload ~/Library/LaunchAgents/com.hugh.claude-tap.plist
launchctl load ~/Library/LaunchAgents/com.hugh.claude-tap.plist
```

**Stop entirely:**
```
launchctl unload ~/Library/LaunchAgents/com.hugh.claude-tap.plist
```

**Logs:** `~/Library/Logs/claude-tap.log`

**Marking a tmux window as the target:** `C-b ,` then rename to anything ending in `-x` (e.g., `claude-x`).
```

- [ ] **Step 6: Commit**

```bash
git -C ~/src/claude-tap add launchd/ bootstrap.sh README.md
git -C ~/src/claude-tap commit -m "Add launchd plist and bootstrap"
```

---

## Self-Review Notes

- **Spec coverage:** Architecture (Task 1+3), components (all tasks), edge cases including disconnect/lizard-reversion (Task 4), launchd packaging (Task 5), Input Monitoring permission (Task 1 step 4 + bootstrap message). Out-of-scope items (X/Y mapping, tiebreaker for multiple `-x` windows, status indicator) are explicitly deferred in the spec and not represented as tasks — correct.
- **Placeholder scan:** No TBDs. The button-bit assumption in Task 2 is hedged with explicit "if your captured bytes differ, adjust" guidance — that's a verified-on-hardware caveat, not a placeholder.
- **Type/name consistency:** `parse_buttons`, `open_controller`, `resolve_target`, `send_keystroke`, `event_loop`, `run_forever`, `BUTTON_BITS`, `BUTTON_TO_DIGIT` — all referenced consistently across tasks.
