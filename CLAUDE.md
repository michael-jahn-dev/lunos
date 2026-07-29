# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Lunos reads lux values from a Lunar-compatible ESP32 ambient-light sensor over its SSE stream and drives an external monitor's brightness to match. Three top-level modules, no package structure:

- `main.py` — the daemon: config schema + overlay, backends, filtering, the `Daemon` loop. This is where the behavior lives.
- `control.py` — the control socket (stdlib-only IPC surface the tray talks to). Imported by `main.py`; imports nothing from it.
- `tray.py` — the optional PySide6 tray app. A pure client: it imports nothing from `main.py`/`control.py` and learns everything over the wire.

Tests live in `tests/` (stdlib `unittest`): `test_main.py` (daemon logic), `test_control.py` (socket protocol), `test_tray.py` (skipped unless PySide6 is installed).

## Commands

```sh
./install.sh                                     # create venv/, pip install requirements, register + (re)start the systemd user service
./install.sh --with-tray                         # ...plus lunos-tray.service and the .desktop file (needs system python3-pyside6)
venv/bin/python3 main.py                         # run the daemon in the foreground (useful for iterating; watch stdout logs)
python3 tray.py                                  # run the tray app in the foreground (system interpreter, not the venv)
systemctl --user status lunos.service            # service status
journalctl --user -u lunos.service -f            # follow live logs
systemctl --user restart lunos.service           # apply changes after editing main.py
venv/bin/python3 -m unittest discover -s tests -t .   # run all unit tests (from the repo root)
socat - UNIX-CONNECT:$XDG_RUNTIME_DIR/lunos/control.sock   # drive the control socket by hand
```

There is no build or lint tooling. `install.sh` is idempotent: it reuses an existing `venv/`, rewrites the unit files, and restarts the services each run — so re-running it also applies changes to `main.py`.

**Run the tests after every change** (`venv/bin/python3 -m unittest discover -s tests -t .`, from the repo root — `-t .` is what keeps `import main` working) and confirm they pass before considering the change done. The tests use fakes for the monitor and sensor, so they need no hardware, `busctl`, or `ddcutil` — but they do import `main`, so run them through the venv (which has `requests`/`sseclient`). When you change or add behavior, add or update the corresponding test in the same edit.

## Configuration model

The frozen `Config` dataclass at the top of `main.py` is the **schema, the defaults and the documentation**. `~/.config/lunos/config.json` is an **overrides-only overlay** on top of it (`load_config()` / `save_config()`; `LUNOS_CONFIG_FILE` overrides the path). An absent file means pure defaults — that is the backwards-compatibility guarantee, so never make the daemon depend on the file existing.

- Adding a `Config` field **requires** a matching entry in `FIELD_SPECS` (type, range, unit, section, and `apply` class). A test asserts the two stay in sync. That one table drives validation, the apply matrix, and the settings window the tray generates from `get_schema`.
- **The daemon is the only writer of `config.json`.** The tray never writes it; it sends `set_config` and the daemon validates → applies → persists atomically (write-then-`os.replace`, same as `_save_offset()`). Don't add a second writer.
- All validation is server-side, in `coerce_config_value()`: whitelist against `FIELD_SPECS`, type- and range-check, never `setattr` an arbitrary key. The tray's own checks are a convenience only.
- `apply` classes: `hot` (visible next loop iteration), `reconnect` (sensor fields — the running `read_ambient_lux_values()` generator captured the old `Config`, so the stream must be re-opened), `restart` (`default_bucket_index`, read only at startup).

## Architecture

`Daemon` (in `main.py`) owns the reconnect loop around the SSE generator, feeding a filter → bucket-selection → backend pipeline. Key seams:

- **Backends (`MonitorController`)** — brightness is applied through one of two interchangeable backends chosen by `MonitorController._select_backend()`:
  - `PowerDevilBackend` (preferred when `prefer_powerdevil` and detected): talks to KDE Plasma 6's `org.kde.ScreenBrightness` D-Bus service **via the `busctl` CLI** (not a Python D-Bus binding — deliberate, to avoid a dependency needed on only one path). Going through PowerDevil keeps Plasma's own slider/OSD in sync and avoids two programs racing over DDC/CI. `supports_ramping = False` because PowerDevil already debounces its own writes.
  - `DdcutilBackend`: shells out to `ddcutil setvcp/getvcp 10` (VCP code 10 = brightness). `supports_ramping = True`; Lunos does its own capped ramp (`ramp_to`) since raw ddcutil doesn't debounce.
  - `ramp_to()` branches on `backend.supports_ramping`: one instant call on PowerDevil, a bounded staircase (≤ `max_transition_steps`) on ddcutil.
- **Lux → brightness mapping** — `select_bucket_index()` over the overlapping `Config.buckets` table. The overlap *is* the hysteresis: a reading still inside the current bucket never changes brightness. `LuxMedianFilter` smooths raw samples first (separate concern from bucket hysteresis).
- **Manual-override handling (`ManualOverrideGuard`)** — macOS-style. Polls actual vs. last-applied brightness; a mismatch beyond tolerance pauses auto-adjust for a cooldown, adopts the manual value as the new baseline, and records a standing `offset_pct` added to all future targets. The offset (only — not the cooldown) is persisted to `Config.offset_state_file` (default `~/.local/state/lunos/offset.json`) and restored at startup; `None` disables. Note the poll clock is seeded to `time.monotonic()` (not 0) to avoid a false override at boot. `set_offset()` is the tray's path: explicit intent, so it clears the cooldown and never round-trips the value through the monitor the way `record_override()` does.
- **Sensor stream (`read_ambient_lux_values`)** — generator over `sseclient`. Filters to `Config.sensor_event_id`; non-JSON lines are firmware log output (surfaced via `log()`, not errors); raises `StaleSensorData` if connected but silent past `stale_reading_timeout_seconds` so the outer loop reconnects.
- **Threading** — the loop thread owns *all* mutable state. `control.py` runs a `ThreadingUnixStreamServer` in a daemon thread and never touches it: `Daemon.dispatch()` answers read-only commands inline and queues mutating ones, which the loop drains at the top of each iteration; `_publish_snapshot()` publishes a plain dict under a lock. Consequence: commands take effect within ~1 lux reading. Don't "fix" that with locks around the loop's state.

## Gotchas

- `sensor_event_id` (default `sensor-ambient_light`) must match the *device's actual firmware id*, which differs from lunar.fyi's generic docs example. Verify against a real device with `curl -N lunarsensor.local/events`.
- Backend selection, ramping, and OSD/notification suppression all key off the backend type — when touching one, check `MonitorController.shows_native_osd` and the `supports_ramping` flags stay consistent. `set_config()` must forward to **the backend instance** too: `monitor_display` and `powerdevil_show_osd` are read there, so a swap that stops at the controller makes those two settings silently do nothing.
- The startup path re-anchors `current_bucket_index` to the monitor's *actual* current brightness (`nearest_bucket_index_for_pct`), not a fixed default — don't reintroduce a hardcoded starting bucket. Same re-anchoring is required after a bucket-table change or a backend switch.
- `run()` only acts on *bucket transitions*, so anything else that changes the target (an offset change, a new curve) must set `Daemon._target_dirty` or it silently does nothing until the light changes.
- **DBusMenu carries menu items, not widgets.** `QWidgetAction` (a slider in the tray menu) renders in a local Qt popup and is silently dropped by the real Plasma tray. The offset radio submenu is the workaround; real sliders belong in the settings window.
- **Overlapping buckets are correct.** The tray's curve editor warns when adjacent buckets *don't* overlap. Inverting that warning would push users into a visibly flickering curve — it is the single most damaging possible bug in that dialog.
- `control.py` deliberately imports nothing from `main.py`: `main.py` runs as a script, so importing it back by name would create a second module object with its own `Config` class. Everything the server needs is duck-typed off the daemon object it is handed.
- Bump `PROTOCOL_VERSION` (in `main.py`, mirrored in `tray.py`) whenever the command set or the snapshot shape changes; both halves check it so mismatched versions fail loudly.
