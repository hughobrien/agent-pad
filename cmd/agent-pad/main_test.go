// Copyright (C) 2026 Hugh O'Brien
// SPDX-License-Identifier: GPL-3.0-or-later
package main

import "testing"

// hexReport decodes a captured input report from its hex string.
func hexReport(t *testing.T, s string) []byte {
	t.Helper()
	if len(s)%2 != 0 {
		t.Fatalf("odd-length hex %q", s)
	}
	b := make([]byte, len(s)/2)
	for i := range b {
		var v int
		for j := 0; j < 2; j++ {
			c := s[i*2+j]
			switch {
			case c >= '0' && c <= '9':
				v = v<<4 | int(c-'0')
			case c >= 'a' && c <= 'f':
				v = v<<4 | int(c-'a'+10)
			default:
				t.Fatalf("bad hex %q", s)
			}
		}
		b[i] = byte(v)
	}
	return b
}

// All fixtures are real reports captured from a 2015 Steam Controller over BLE
// (AGENT_PAD_DEBUG=1). The key insight: byte[1] is a segment-flags byte. The
// button bitmap (at data[3..5]) is only present when byte[1]&0x10 is set.
// Analog reports (triggers, joystick, trackpads) share byte[2]==0x00 with
// button reports, so the old byte[2] gate misread their analog bytes as button
// presses — that was the bug behind "joystick/left trigger fire events".
func TestParseButtons(t *testing.T) {
	cases := []struct {
		name    string
		hex     string
		wantVal uint32
		wantOK  bool
	}{
		// Real button reports (byte[1]&0x10 set): bitmap at data[3..5].
		{"A", "c0140080000000000000000000000000000000", btnA, true},
		{"B", "c0140020000000000000000000000000000000", btnB, true},
		{"Y", "c0140010000000000000000000000000000000", btnY, true},
		// Combined report: button segment present (none pressed) + analog tail.
		{"stick-click+analog", "c094000000406d0073f8000000000000000000", btnStick, true},

		// Non-button reports (byte[1]&0x10 clear) must NOT be parsed as buttons.
		// Each of these previously slipped through the byte[2]==0x00 gate and
		// the analog bytes set spurious A/B/Y bits.
		{"trigger-analog", "c0040000000000000000000000000000000000", 0, false},
		{"idle-status", "c0055502b2f7010000000000650b0000000000", 0, false},
		{"joystick-analog", "c0240006000000000000000000000000000000", 0, false},
		// data[3..5]=ab17da: 0xab>=0x80 set the A bit under the old parse.
		{"trackpad-analog", "c08400ab17daf9000000000000000000000000", 0, false},

		// Degenerate.
		{"too-short", "c01400", 0, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			val, ok := parseButtons(hexReport(t, tc.hex))
			if ok != tc.wantOK {
				t.Fatalf("ok = %v, want %v", ok, tc.wantOK)
			}
			if ok && val != tc.wantVal {
				t.Fatalf("val = 0x%06x, want 0x%06x", val, tc.wantVal)
			}
		})
	}
}

// Regression guard: a left-trigger analog sweep must never emit a digit. Under
// the old gate, analog bytes >=0x80 in data[3] tripped btnA -> "1".
func TestAnalogSweepEmitsNothing(t *testing.T) {
	for v := 0; v <= 0xff; v++ {
		// byte[1]=0x84 (analog, no 0x10 flag), analog value in data[3].
		r := []byte{0xc0, 0x84, 0x00, byte(v), 0x00, 0x00}
		if _, ok := parseButtons(r); ok {
			t.Fatalf("analog report with data[3]=0x%02x parsed as buttons", v)
		}
	}
}
