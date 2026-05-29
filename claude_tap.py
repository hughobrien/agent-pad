"""claude-tap: Steam Controller (BLE) -> tmux keystroke bridge.

Architecture:
- Controller is already bonded to macOS as a HID device. We get parallel access
  to its vendor GATT service via CoreBluetooth's retrieveConnectedPeripheralsWithServices.
- On connect, send the one-shot disable-lizard command. macOS stops seeing
  global keystrokes/mouse from the controller.
- Subscribe to GATT input notifications. Button-state reports have byte[2]=0x00,
  with byte[3] holding a bitmask: A=0x80, B=0x20.
- On a press edge for A, send '1' to the first tmux window whose name ends in
  -x. For B, send '2'.
"""
from __future__ import annotations

import logging
import subprocess
import sys

import objc
from CoreBluetooth import CBCentralManager, CBUUID
from Foundation import NSObject, NSRunLoop, NSDate, NSData


log = logging.getLogger("claude-tap")

VENDOR_SVC_UUID  = CBUUID.UUIDWithString_("100f6c32-1735-4313-b402-38567131e5f3")
INPUT_CHAR_UUID  = CBUUID.UUIDWithString_("100f6c33-1735-4313-b402-38567131e5f3")
OUTPUT_CHAR_UUID = CBUUID.UUIDWithString_("100f6c34-1735-4313-b402-38567131e5f3")

# Single-segment framing (0xC0) + SET_SETTINGS (0x87) + 3 bytes follow + reg 0x08 + val 0x07 + pad
DISABLE_LIZARD = bytes([0xC0, 0x87, 0x03, 0x08, 0x07, 0x00])

# Full 24-bit button bitmap, formed by combining bytes[3..5] of a GATT
# button report (byte[2] == 0x00). Reference:
#   https://dennis-hamester.gitlab.io/scraw/protocol/
# Only A and B are wired up today; rest documented for future expansion.
BUTTON_BITS = {
    "RIGHT_GRIP":          0x000001,
    "LEFT_TRACKPAD_STICK": 0x000002,
    "RIGHT_TRACKPAD":      0x000004,
    "LEFT_TRACKPAD_TOUCH": 0x000008,
    "RIGHT_TRACKPAD_TOUCH":0x000010,
    "STICK":               0x000040,
    "SELECT":              0x001000,
    "STEAM":               0x002000,
    "START":               0x004000,
    "LEFT_GRIP":           0x008000,
    "RIGHT_TRIGGER":       0x010000,
    "LEFT_TRIGGER":        0x020000,
    "RIGHT_SHOULDER":      0x040000,
    "LEFT_SHOULDER":       0x080000,
    "Y":                   0x100000,
    "B":                   0x200000,
    "X":                   0x400000,
    "A":                   0x800000,
}

# What each button sends. Add entries to extend.
BUTTON_TO_DIGIT = {"A": "1", "B": "2", "Y": "3"}

RECONNECT_PUMP_INTERVAL_S = 0.05


def resolve_tmux_target() -> str | None:
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
    target = resolve_tmux_target()
    if target is None:
        log.debug("no -x window; dropping %s", digit)
        return
    log.info("sending %s -> %s", digit, target)
    subprocess.run(["tmux", "send-keys", "-t", target, digit], check=False)


class ClaudeTapDelegate(NSObject):
    """CBCentralManager + CBPeripheral delegate. Owns reconnect lifecycle."""

    def init(self):
        self = objc.super(ClaudeTapDelegate, self).init()
        if self is None:
            return None
        self.cm = None
        self.peripheral = None
        self.input_char = None
        self.output_char = None
        self.prev_buttons: int = 0
        return self

    def centralManagerDidUpdateState_(self, central):
        state = central.state()
        log.info("CB state: %d", state)
        if state == 5:  # CBManagerStatePoweredOn
            self._try_attach()

    def _try_attach(self) -> None:
        peripherals = self.cm.retrieveConnectedPeripheralsWithServices_(
            [VENDOR_SVC_UUID]
        )
        if not peripherals:
            log.warning("No Steam Controller found among connected peripherals. "
                        "Is it bonded as a HID device?")
            return
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
        log.error("Connect failed: %s — retrying", e)
        self.cm.connectPeripheral_options_(p, None)

    def centralManager_didDisconnectPeripheral_error_(self, c, p, e):
        log.warning("Disconnected (%s) — reconnecting", e)
        self.input_char = None
        self.output_char = None
        self.prev_buttons = 0
        self.cm.connectPeripheral_options_(p, None)

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

        if self.output_char is not None:
            data = NSData.dataWithBytes_length_(DISABLE_LIZARD, len(DISABLE_LIZARD))
            peripheral.writeValue_forCharacteristic_type_(
                data, self.output_char, 0,
            )
            log.info("Sent disable-lizard")
        if self.input_char is not None:
            peripheral.setNotifyValue_forCharacteristic_(True, self.input_char)
            log.info("Subscribed to input notifications")

    def peripheral_didUpdateValueForCharacteristic_error_(
        self, peripheral, ch, error
    ):
        if error:
            return
        v = ch.value()
        if v is None or len(v) < 4:
            return
        data = bytes(v)
        # Button reports are byte[2]==0x00, with bytes[3..5] holding the 24-bit
        # button bitmap (little-endian: byte[3] is the high byte per measurement).
        if data[2] != 0x00 or len(data) < 6:
            return
        buttons = (data[3] << 16) | (data[4] << 8) | data[5]
        for name, digit in BUTTON_TO_DIGIT.items():
            mask = BUTTON_BITS[name]
            was = self.prev_buttons & mask
            now = buttons & mask
            if not was and now:  # 0 -> 1 edge = press
                send_keystroke(digit)
        self.prev_buttons = buttons


def run_forever() -> None:
    delegate = ClaudeTapDelegate.alloc().init()
    cm = CBCentralManager.alloc().initWithDelegate_queue_(delegate, None)
    delegate.cm = cm
    rl = NSRunLoop.currentRunLoop()
    log.info("Event loop started")
    while True:
        rl.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(RECONNECT_PUMP_INTERVAL_S))


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
