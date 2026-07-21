# System tray app (Fedora)

**Status:** Draft — to be refined
**Tracking issue:** [#2](https://github.com/michael-jahn-dev/lunos/issues/2)

## Summary

A Linux (Fedora) system-tray application that puts Lunos's settings behind a GUI
instead of the hardcoded `Config` dataclass in `main.py`. It also exposes a live,
adjustable manual brightness offset — surfacing what `ManualOverrideGuard.offset_pct`
already does internally — so the user can nudge the auto-brightness up or down
without editing code. This removes today's edit-`main.py`-and-restart loop.

## Motivation

Configuration currently requires editing the `Config` dataclass and restarting the
service. A tray app makes settings and the manual offset adjustable at runtime by
anyone, not just someone comfortable editing Python.

## Rough scope

- **Tray applet** — an icon + menu living in the system tray.
- **Settings UI** — surface the `Config` fields (buckets, timings, backend
  preference, notifications, etc.) as editable controls.
- **Adjustable offset** — a control to raise/lower brightness relative to the
  auto-picked value, wired to the daemon's offset logic.
- **Daemon ↔ app communication** — a way for the app to push settings/offset to
  the running daemon and read its current state.

## Open questions

- GUI toolkit (Qt/KDE-native, GTK, something lightweight)?
- How do the app and daemon talk (D-Bus, a socket, a shared config file the daemon
  watches)?
- Does config move out of the `Config` dataclass into a persisted file, and how do
  the two stay in sync?
- Does the tray app also start/stop/monitor the systemd service?
