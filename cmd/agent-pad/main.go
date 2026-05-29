// agent-pad: Steam Controller (BLE) -> tmux keystroke bridge.
//
// See docs/specs/2026-05-29-agent-pad-design.md for the architecture and
// exact GATT byte sequences. Short version: we hold a CoreBluetooth
// connection to the Steam Controller's vendor service in parallel with macOS
// owning it as a HID device, write the disable-lizard command, and translate
// GATT button notifications into tmux send-keys.
package main

import (
	"bytes"
	"log"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/tinygo-org/cbgo"
)

var (
	vendorSvcUUID  = cbgo.MustParseUUID("100f6c32-1735-4313-b402-38567131e5f3")
	inputCharUUID  = cbgo.MustParseUUID("100f6c33-1735-4313-b402-38567131e5f3")
	outputCharUUID = cbgo.MustParseUUID("100f6c34-1735-4313-b402-38567131e5f3")
)

// Single-segment framing 0xC0 + SET_SETTINGS 0x87 + 3-byte arg + reg LEFT_TRACKPAD_MODE 0x08 + value 0x07.
var disableLizard = []byte{0xC0, 0x87, 0x03, 0x08, 0x07, 0x00}

// 24-bit button bitmap formed by combining bytes[3..5] of a button report
// (byte[2]==0x00). See https://dennis-hamester.gitlab.io/scraw/protocol/.
const (
	btnRightGrip          uint32 = 0x000001
	btnLeftTrackpadStick  uint32 = 0x000002
	btnRightTrackpad      uint32 = 0x000004
	btnLeftTrackpadTouch  uint32 = 0x000008
	btnRightTrackpadTouch uint32 = 0x000010
	btnStick              uint32 = 0x000040
	btnSelect             uint32 = 0x001000
	btnSteam              uint32 = 0x002000
	btnStart              uint32 = 0x004000
	btnLeftGrip           uint32 = 0x008000
	btnRightTrigger       uint32 = 0x010000
	btnLeftTrigger        uint32 = 0x020000
	btnRightShoulder      uint32 = 0x040000
	btnLeftShoulder       uint32 = 0x080000
	btnY                  uint32 = 0x100000
	btnB                  uint32 = 0x200000
	btnX                  uint32 = 0x400000
	btnA                  uint32 = 0x800000
)

// What each button sends. Add entries to extend.
var buttonToDigit = []struct {
	mask  uint32
	digit string
}{
	{btnA, "1"},
	{btnB, "2"},
	{btnY, "3"},
}

const (
	cbPoweredOn          = cbgo.ManagerStatePoweredOn
	reattachPollInterval = 2 * time.Second
)

// tmuxBin is resolved at startup. Prefers $TMUX_BIN (set by the launchd plist
// at install time), falls back to PATH lookup.
var tmuxBin = resolveTmux()

func resolveTmux() string {
	if p := os.Getenv("TMUX_BIN"); p != "" {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	if p, err := exec.LookPath("tmux"); err == nil {
		return p
	}
	log.Print("warning: tmux not found via TMUX_BIN env or PATH; sends will fail")
	return "tmux"
}

func resolveTmuxTarget() string {
	out, err := exec.Command(tmuxBin, "list-windows", "-a",
		"-F", "#{session_name}:#{window_index} #{window_name}").Output()
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(out), "\n") {
		sp := strings.IndexByte(line, ' ')
		if sp < 0 {
			continue
		}
		target, name := line[:sp], line[sp+1:]
		if strings.HasSuffix(name, "-x") {
			return target
		}
	}
	return ""
}

func sendKeystroke(digit string) {
	target := resolveTmuxTarget()
	if target == "" {
		return
	}
	log.Printf("sending %s -> %s", digit, target)
	if err := exec.Command(tmuxBin, "send-keys", "-t", target, digit).Run(); err != nil {
		log.Printf("send-keys failed: %v", err)
	}
}

// agent is both the CentralManagerDelegate and PeripheralDelegate. The
// embedded *Base types provide no-op implementations of methods we don't
// override.
type agent struct {
	cbgo.CentralManagerDelegateBase
	cbgo.PeripheralDelegateBase

	cm cbgo.CentralManager

	mu            sync.Mutex
	inputChar     *cbgo.Characteristic
	outputChar    *cbgo.Characteristic
	prevButtons   uint32
	attached      bool
	waitingLogged bool
}

func (a *agent) clearState() {
	a.inputChar = nil
	a.outputChar = nil
	a.prevButtons = 0
	a.attached = false
}

func (a *agent) CentralManagerDidUpdateState(cmgr cbgo.CentralManager) {
	state := cmgr.State()
	log.Printf("CB state: %d", state)
	a.mu.Lock()
	defer a.mu.Unlock()
	if state == cbPoweredOn {
		a.tryAttachLocked()
	} else {
		a.clearState()
	}
}

func (a *agent) tryAttachLocked() {
	prphs := a.cm.RetrieveConnectedPeripheralsWithServices([]cbgo.UUID{vendorSvcUUID})
	if len(prphs) == 0 {
		if !a.waitingLogged {
			log.Printf("Steam Controller not currently connected to macOS. "+
				"Will poll every %s; wake it and it'll appear.", reattachPollInterval)
			a.waitingLogged = true
		}
		return
	}
	a.waitingLogged = false
	p := prphs[0]
	log.Printf("Attaching to %s (id=%s)", p.Name(), p.Identifier().String())
	a.cm.Connect(p, nil)
}

func (a *agent) DidConnectPeripheral(cmgr cbgo.CentralManager, prph cbgo.Peripheral) {
	log.Print("Connected")
	prph.SetDelegate(a)
	prph.DiscoverServices([]cbgo.UUID{vendorSvcUUID})
}

func (a *agent) DidFailToConnectPeripheral(cmgr cbgo.CentralManager, prph cbgo.Peripheral, err error) {
	// Log only. Retrying here causes a tight loop when CB is offline.
	log.Printf("Connect failed: %v", err)
}

func (a *agent) DidDisconnectPeripheral(cmgr cbgo.CentralManager, prph cbgo.Peripheral, err error) {
	log.Printf("Disconnected (%v) — will poll until peripheral returns", err)
	a.mu.Lock()
	defer a.mu.Unlock()
	a.clearState()
}

func (a *agent) DidDiscoverServices(prph cbgo.Peripheral, err error) {
	if err != nil {
		log.Printf("Service discovery failed: %v", err)
		return
	}
	for _, svc := range prph.Services() {
		if bytes.Equal(svc.UUID(), vendorSvcUUID) {
			prph.DiscoverCharacteristics(
				[]cbgo.UUID{inputCharUUID, outputCharUUID}, svc)
			return
		}
	}
}

func (a *agent) DidDiscoverCharacteristics(prph cbgo.Peripheral, svc cbgo.Service, err error) {
	if err != nil {
		log.Printf("Char discovery failed: %v", err)
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	for _, ch := range svc.Characteristics() {
		switch {
		case bytes.Equal(ch.UUID(), inputCharUUID):
			a.inputChar = &ch
		case bytes.Equal(ch.UUID(), outputCharUUID):
			a.outputChar = &ch
		}
	}
	if a.inputChar == nil || a.outputChar == nil {
		log.Print("expected vendor chars not found; staying detached")
		return
	}
	prph.WriteCharacteristic(disableLizard, *a.outputChar, true)
	log.Print("Sent disable-lizard")
	prph.SetNotify(true, *a.inputChar)
	log.Print("Subscribed to input notifications")
	a.attached = true
}

func (a *agent) DidUpdateValueForCharacteristic(prph cbgo.Peripheral, ch cbgo.Characteristic, err error) {
	if err != nil {
		return
	}
	data := ch.Value()
	if len(data) < 6 || data[2] != 0x00 {
		return
	}
	buttons := uint32(data[3])<<16 | uint32(data[4])<<8 | uint32(data[5])
	a.mu.Lock()
	prev := a.prevButtons
	a.prevButtons = buttons
	a.mu.Unlock()
	for _, b := range buttonToDigit {
		if prev&b.mask == 0 && buttons&b.mask != 0 {
			sendKeystroke(b.digit)
		}
	}
}

func main() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	log.Print("agent-pad starting")

	a := &agent{}
	a.cm = cbgo.NewCentralManager(nil)
	a.cm.SetDelegate(a)

	// SIGTERM/SIGINT handler so launchd can stop us cleanly.
	sigs := make(chan os.Signal, 1)
	signal.Notify(sigs, os.Interrupt, syscall.SIGTERM)
	done := make(chan struct{})
	go func() {
		<-sigs
		log.Print("stopping")
		close(done)
	}()

	// Skip the very first poll so the CB state-update callback owns the
	// initial attach; otherwise both fire within the same tick.
	lastPoll := time.Now()
	ticker := time.NewTicker(50 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-done:
			return
		case <-ticker.C:
			if a.cm.State() != cbPoweredOn {
				continue
			}
			a.mu.Lock()
			needPoll := !a.attached && time.Since(lastPoll) > reattachPollInterval
			a.mu.Unlock()
			if needPoll {
				lastPoll = time.Now()
				a.mu.Lock()
				a.tryAttachLocked()
				a.mu.Unlock()
			}
		}
	}
}
