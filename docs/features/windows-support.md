# Windows support

**Status:** Draft — to be refined
**Tracking issue:** [#1](https://github.com/michael-jahn-dev/lunos/issues/1)

## Summary

Make Lunos run on Windows, not just Linux. The sensor pipeline (SSE stream, lux
filtering, bucket mapping) is already platform-agnostic; what's Linux-specific is
how brightness is applied, how the daemon runs in the background, and how
notifications are shown. Windows support means providing a Windows path for each
of those three seams while leaving the core loop unchanged.

## Motivation

Broaden Lunos beyond Linux so the same ambient-light sensor can drive an external
monitor's brightness on a Windows machine.

## Rough scope

- **Brightness backend** — a Windows backend alongside `DdcutilBackend` /
  `PowerDevilBackend`, likely using the Windows Monitor Configuration API
  (`GetMonitorBrightness` / `SetMonitorBrightness`) for external DDC/CI monitors.
- **Service model** — replace the systemd user service with a Windows
  background/startup mechanism (scheduled task, startup entry, or a service).
- **Notifications** — replace `notify-send` with Windows toast notifications.
- **Backend selection** — extend `MonitorController` to pick the Windows backend
  by platform.

## Open questions

- Which API/library for DDC/CI brightness on Windows (raw Win32 via `ctypes`, a
  wrapper like `monitorcontrol`, something else)?
- How should the daemon be installed and kept running on Windows (parallel to
  `install.sh`)?
- Packaging: ship as a Python script + venv like today, or a bundled executable?
