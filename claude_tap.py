"""claude-tap: Steam Controller (BLE) -> tmux keystroke bridge.

See docs/specs/2026-05-29-claude-tap-design.md for the architecture and the
exact GATT byte sequences. In short: we hold a CoreBluetooth connection to
the Steam Controller's vendor service in parallel with macOS owning it as a
HID device, write the disable-lizard command, and translate GATT button
notifications into tmux send-keys.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time

import objc
from CoreBluetooth import CBCentralManager, CBUUID
from Foundation import NSObject, NSRunLoop, NSDate, NSData


log = logging.getLogger("claude-tap")

VENDOR_SVC_UUID  = CBUUID.UUIDWithString_("100f6c32-1735-4313-b402-38567131e5f3")
INPUT_CHAR_UUID  = CBUUID.UUIDWithString_("100f6c33-1735-4313-b402-38567131e5f3")
OUTPUT_CHAR_UUID = CBUUID.UUIDWithString_("100f6c34-1735-4313-b402-38567131e5f3")

# Single-segment framing 0xC0 + SET_SETTINGS 0x87 + 3-byte arg + reg LEFT_TRACKPAD_MODE 0x08 + value 0x07.
DISABLE_LIZARD = bytes([0xC0, 0x87, 0x03, 0x08, 0x07, 0x00])

CB_POWERED_ON = 5  # CBManagerStatePoweredOn
WRITE_WITH_RESPONSE = 0  # CBCharacteristicWriteWithResponse

# 24-bit button bitmap formed by combining bytes[3..5] of a button report
# (byte[2]==0x00). See https://dennis-hamester.gitlab.io/scraw/protocol/.
BUTTON_BITS = {
    "RIGHT_GRIP":           0x000001,
    "LEFT_TRACKPAD_STICK":  0x000002,
    "RIGHT_TRACKPAD":       0x000004,
    "LEFT_TRACKPAD_TOUCH":  0x000008,
    "RIGHT_TRACKPAD_TOUCH": 0x000010,
    "STICK":                0x000040,
    "SELECT":               0x001000,
    "STEAM":                0x002000,
    "START":                0x004000,
    "LEFT_GRIP":            0x008000,
    "RIGHT_TRIGGER":        0x010000,
    "LEFT_TRIGGER":         0x020000,
    "RIGHT_SHOULDER":       0x040000,
    "LEFT_SHOULDER":        0x080000,
    "Y":                    0x100000,
    "B":                    0x200000,
    "X":                    0x400000,
    "A":                    0x800000,
}

# What each button sends. Add entries to extend.
BUTTON_TO_DIGIT = {"A": "1", "B": "2", "Y": "3"}

RUNLOOP_TICK_S = 0.05
REATTACH_POLL_INTERVAL_S = 2.0


def find_tmux() -> str:
    """Resolve tmux's absolute path. Prefers $TMUX_BIN set by the launchd plist
    (populated by bootstrap.sh from the user's interactive shell). Falls back
    to PATH for interactive runs.
    """
    env_path = os.environ.get("TMUX_BIN")
    if env_path and os.path.exists(env_path):
        return env_path
    found = shutil.which("tmux")
    if found:
        return found
    log.warning("tmux not found via TMUX_BIN env or PATH; sends will fail.")
    return "tmux"


TMUX = find_tmux()


def resolve_tmux_target() -> str | None:
    """Return tmux target spec for the first window whose name ends in -x."""
    result = subprocess.run(
        [TMUX, "list-windows", "-a",
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
    try:
        target = resolve_tmux_target()
        if target is None:
            log.debug("no -x window; dropping %s", digit)
            return
        log.info("sending %s -> %s", digit, target)
        subprocess.run([TMUX, "send-keys", "-t", target, digit], check=False)
    except Exception:
        log.exception("send_keystroke failed for %r", digit)


class ClaudeTapDelegate(NSObject):
    """CBCentralManager + CBPeripheral delegate. Owns reconnect lifecycle."""

    def init(self):
        self = objc.super(ClaudeTapDelegate, self).init()
        if self is None:
            return None
        self.cm = None
        self._clear_state()
        self._waiting_logged = False
        return self

    def _clear_state(self) -> None:
        self.peripheral = None
        self.input_char = None
        self.output_char = None
        self.prev_buttons = 0
        self.attached = False

    def centralManagerDidUpdateState_(self, central):
        state = central.state()
        log.info("CB state: %d", state)
        if state == CB_POWERED_ON:
            self._try_attach()
        else:
            self._clear_state()

    def _try_attach(self) -> None:
        peripherals = self.cm.retrieveConnectedPeripheralsWithServices_(
            [VENDOR_SVC_UUID]
        )
        if not peripherals:
            if not self._waiting_logged:
                log.warning("Steam Controller not currently connected to macOS. "
                            "Will poll every %ss; wake it and it'll appear.",
                            REATTACH_POLL_INTERVAL_S)
                self._waiting_logged = True
            return
        self._waiting_logged = False
        self.peripheral = peripherals[0]
        log.info("Attaching to %s (id=%s)",
                 self.peripheral.name(),
                 self.peripheral.identifier().UUIDString())
        self.cm.connectPeripheral_options_(self.peripheral, None)

    def centralManager_didConnectPeripheral_(self, c, p):
        log.info("Connected")
        p.setDelegate_(self)
        p.discoverServices_([VENDOR_SVC_UUID])

    def centralManager_didFailToConnectPeripheral_error_(self, c, p, e):
        # Logging only; retrying here causes a tight loop when CB is offline.
        log.error("Connect failed: %s", e)

    def centralManager_didDisconnectPeripheral_error_(self, c, p, e):
        log.warning("Disconnected (%s) — will poll until peripheral returns", e)
        self._clear_state()

    def peripheral_didDiscoverServices_(self, peripheral, error):
        if error:
            log.error("Service discovery failed: %s", error)
            return
        for svc in peripheral.services():
            if svc.UUID() == VENDOR_SVC_UUID:
                peripheral.discoverCharacteristics_forService_(
                    [INPUT_CHAR_UUID, OUTPUT_CHAR_UUID], svc
                )
                return

    def peripheral_didDiscoverCharacteristicsForService_error_(
        self, peripheral, svc, error
    ):
        if error:
            log.error("Char discovery failed: %s", error)
            return
        for ch in svc.characteristics():
            if ch.UUID() == INPUT_CHAR_UUID:
                self.input_char = ch
            elif ch.UUID() == OUTPUT_CHAR_UUID:
                self.output_char = ch

        if self.output_char is None or self.input_char is None:
            log.error("Expected vendor chars not found; staying detached.")
            return

        data = NSData.dataWithBytes_length_(DISABLE_LIZARD, len(DISABLE_LIZARD))
        peripheral.writeValue_forCharacteristic_type_(
            data, self.output_char, WRITE_WITH_RESPONSE,
        )
        log.info("Sent disable-lizard")
        peripheral.setNotifyValue_forCharacteristic_(True, self.input_char)
        log.info("Subscribed to input notifications")
        self.attached = True

    def peripheral_didUpdateValueForCharacteristic_error_(
        self, peripheral, ch, error
    ):
        if error:
            return
        v = ch.value()
        if v is None or len(v) < 6:
            return
        data = bytes(v)
        if data[2] != 0x00:
            return
        buttons = (data[3] << 16) | (data[4] << 8) | data[5]
        for name, digit in BUTTON_TO_DIGIT.items():
            mask = BUTTON_BITS[name]
            if not (self.prev_buttons & mask) and (buttons & mask):
                send_keystroke(digit)
        self.prev_buttons = buttons


def run_forever() -> None:
    delegate = ClaudeTapDelegate.alloc().init()
    cm = CBCentralManager.alloc().initWithDelegate_queue_(delegate, None)
    delegate.cm = cm
    rl = NSRunLoop.currentRunLoop()
    log.info("Event loop started")
    # Skip the very first poll so the CB state-update callback owns the
    # initial attach; otherwise both fire within the same tick.
    last_attach_poll = time.monotonic()
    while True:
        rl.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(RUNLOOP_TICK_S))
        if not delegate.attached and cm.state() == CB_POWERED_ON:
            now = time.monotonic()
            if now - last_attach_poll > REATTACH_POLL_INTERVAL_S:
                last_attach_poll = now
                delegate._try_attach()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log.info("claude-tap starting")
    try:
        run_forever()
    except KeyboardInterrupt:
        log.info("stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
