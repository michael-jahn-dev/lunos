# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Lunos is a single-file Python daemon (`main.py`) that reads lux values from a Lunar-compatible ESP32 ambient-light sensor over its SSE stream and drives an external monitor's brightness to match. All logic — config, backends, filtering, main loop — lives in `main.py`; there is no package structure and no test suite.

## Commands

```sh
./install.sh                                   # create venv/, pip install requirements, register + start the systemd user service
venv/bin/python3 main.py                        # run directly in the foreground (useful for iterating; watch stdout logs)
systemctl --user status lunos.service           # service status
journalctl --user -u lunos.service -f           # follow live logs
systemctl --user restart lunos.service          # apply changes after editing main.py
```

There is no build, lint, or test tooling. `install.sh` is idempotent: it reuses an existing `venv/` and rewrites the unit file each run.

## Configuration model

There is **no external config file**. Every tunable is a field on the frozen `Config` dataclass at the top of `main.py`, instantiated once in `if __name__ == "__main__"`. To change behavior, edit `Config` defaults and restart the service. The README's Configuration table documents each field.

## Architecture

The daemon is a reconnect loop (`run()`) around an SSE generator, feeding a filter → bucket-selection → backend pipeline. Key seams:

- **Backends (`MonitorController`)** — brightness is applied through one of two interchangeable backends chosen at startup by `MonitorController.__init__`:
  - `PowerDevilBackend` (preferred when `prefer_powerdevil` and detected): talks to KDE Plasma 6's `org.kde.ScreenBrightness` D-Bus service **via the `busctl` CLI** (not a Python D-Bus binding — deliberate, to avoid a dependency needed on only one path). Going through PowerDevil keeps Plasma's own slider/OSD in sync and avoids two programs racing over DDC/CI. `supports_ramping = False` because PowerDevil already debounces its own writes.
  - `DdcutilBackend`: shells out to `ddcutil setvcp/getvcp 10` (VCP code 10 = brightness). `supports_ramping = True`; Lunos does its own capped ramp (`ramp_to`) since raw ddcutil doesn't debounce.
  - `ramp_to()` branches on `backend.supports_ramping`: one instant call on PowerDevil, a bounded staircase (≤ `max_transition_steps`) on ddcutil.
- **Lux → brightness mapping** — `select_bucket_index()` over the overlapping `Config.buckets` table. The overlap *is* the hysteresis: a reading still inside the current bucket never changes brightness. `LuxMedianFilter` smooths raw samples first (separate concern from bucket hysteresis).
- **Manual-override handling (`ManualOverrideGuard`)** — macOS-style. Polls actual vs. last-applied brightness; a mismatch beyond tolerance pauses auto-adjust for a cooldown, adopts the manual value as the new baseline, and records a standing `offset_pct` added to all future targets. Runtime-only, resets on restart. Note the poll clock is seeded to `time.monotonic()` (not 0) to avoid a false override at boot.
- **Sensor stream (`read_ambient_lux_values`)** — generator over `sseclient`. Filters to `Config.sensor_event_id`; non-JSON lines are firmware log output (surfaced via `log()`, not errors); raises `StaleSensorData` if connected but silent past `stale_reading_timeout_seconds` so the outer loop reconnects.

## Gotchas

- `sensor_event_id` (default `sensor-ambient_light`) must match the *device's actual firmware id*, which differs from lunar.fyi's generic docs example. Verify against a real device with `curl -N lunarsensor.local/events`.
- Backend selection, ramping, and OSD/notification suppression all key off the backend type — when touching one, check `MonitorController.shows_native_osd` and the `supports_ramping` flags stay consistent.
- The startup path re-anchors `current_bucket_index` to the monitor's *actual* current brightness (`nearest_bucket_index_for_pct`), not a fixed default — don't reintroduce a hardcoded starting bucket.
