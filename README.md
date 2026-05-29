# claude-tap

Steam Controller → tmux keystroke bridge. Tap A on the controller to send `1` to the active pane of any tmux window whose name ends in `-x`. Tap B to send `2`.

See `docs/specs/2026-05-29-claude-tap-design.md` for the design.

## Setup

```
./bootstrap.sh
```

Then grant Input Monitoring permission to `.venv/bin/python` in System Settings → Privacy & Security.
