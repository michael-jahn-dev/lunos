# Windows support

**Status:** Planned
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

## What is already portable

An audit of `main.py` shows the platform-specific surface is small:

| Component | Portable? | Notes |
|---|---|---|
| `Config`, `Bucket`, bucket selection | ✅ | Pure Python |
| `LuxMedianFilter`, `BrightnessUpdateGate` | ✅ | Pure Python, `time.monotonic()` is cross-platform |
| `ManualOverrideGuard` | ✅ | Only talks to `MonitorController` |
| `read_ambient_lux_values` (SSE) | ✅ | `requests` + `sseclient-py`, both cross-platform |
| `run()` main loop | ✅ | No platform assumptions |
| `DdcutilBackend` | ❌ | Shells out to `ddcutil` (Linux tool) |
| `PowerDevilBackend` + `_busctl_*` | ❌ | KDE/D-Bus via `busctl` (Linux only; `detect()` already fails soft via `FileNotFoundError` → `None`) |
| `notify()` | ❌ | Shells out to `notify-send` |
| `install.sh` / service model | ❌ | venv setup is portable in spirit; systemd unit is not |
| `lunarsensor.local` hostname | ⚠️ | Windows 10+ resolves mDNS natively; may need an IP fallback in `Config.sensor_url` on some networks |

So the work is: one new backend, platform-aware backend selection, a Windows
`notify()` branch, and a Windows install story.

## Design decisions

### 1. Brightness backend: ctypes + Win32 Monitor Configuration API (dxva2)

Windows exposes DDC/CI brightness for external monitors through the
**Monitor Configuration API** in `dxva2.dll`. This is the same class of citizen as
PowerDevil on Plasma: the OS-blessed DDC/CI path, no external tool needed.

Options considered:

| Option | Verdict |
|---|---|
| **`ctypes` against `dxva2.dll`** (chosen) | No new dependency — matches the project's deliberate `busctl`-over-python-dbus choice. ~60 lines of ctypes. |
| `monitorcontrol` pip package | Works (wraps the same dxva2 calls), but adds a dependency for one platform path — exactly what the project avoids. |
| WMI `WmiMonitorBrightness` | **Not viable**: only controls internal laptop panels, not external DDC/CI monitors. |
| Shipping `winddcutil` or similar CLI | Extra install burden, no benefit over direct API calls. |

Win32 call sequence for the new `WindowsDdcBackend`:

1. `user32.EnumDisplayMonitors` → `HMONITOR` handles (one per logical monitor).
   Requires a `ctypes.WINFUNCTYPE` `MONITORENUMPROC` callback to collect handles.
2. `dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR` + `dxva2.GetPhysicalMonitorsFromHMONITOR` → `PHYSICAL_MONITOR` structs (`HANDLE hPhysicalMonitor` + `WCHAR szPhysicalMonitorDescription[128]`).
3. `dxva2.GetMonitorBrightness(handle, &min, &current, &max)` — read path.
4. `dxva2.SetMonitorBrightness(handle, value)` — write path.
5. `dxva2.DestroyPhysicalMonitor(handle)` on teardown / re-enumeration.

Load the DLLs with `ctypes.WinDLL(..., use_last_error=True)` and include
`ctypes.get_last_error()` in error messages — dxva2 functions only return a BOOL,
so without the error code, failures are undiagnosable from logs.

Behavioral notes:

- **Normalization**: `GetMonitorBrightness` returns the monitor's *native* min/current/max — not guaranteed 0–100. Normalize to percent exactly like `PowerDevilBackend` does (`round((current - min) / (max - min) * 100)`), and map back on write. Cache min/max per handle like `_cached_max_brightness`.
- **`supports_ramping = True`**: raw DDC/CI writes with no OS debounce — same situation as `DdcutilBackend`, so Lunos's own capped ramp applies.
- **Monitor selection**: internal laptop panels typically fail `GetMonitorBrightness` (no DDC/CI), which filters them out naturally. Pick the first physical monitor whose brightness read succeeds; add a config field (e.g. `windows_monitor_description_contains: str | None`) matching against `szPhysicalMonitorDescription`, mirroring `powerdevil_display_label_contains`.
- **Stale handles**: physical monitor handles go stale after sleep/hotplug/dock events. On any failed call, destroy handles and re-enumerate once before giving up — the outer loop's error handling (`RuntimeError` → log + notify) already covers repeated failure.
- **No hard fail at startup**: do *not* make `detect()` raise (or the constructor exit the process) when enumeration finds no DDC-capable monitor. At logon the monitor may still be asleep or the dock not yet enumerated; a startup crash would burn through Task Scheduler's limited restart-on-failure budget and leave Lunos dead. Instead, mirror `DdcutilBackend`'s posture: always construct the backend, enumerate lazily on first use, return `None` from `get_current_pct()` / raise `RuntimeError` from `set_pct()` per call, and re-attempt enumeration on the next call. `run()` already has the fallback for this exact case ("Could not read current monitor brightness, assuming X%"), and the main loop keeps retrying — the daemon self-heals once the monitor wakes.
- **Lazy Windows imports**: all `ctypes.windll` access must live inside the backend (constructor/methods), never at module import time — `tests/test_main.py` imports `main` on Linux and must keep working. Wrap the raw dxva2 calls in small module-level functions (like `_busctl_get_property`) so tests can fake them.

### 2. Backend selection: platform gate in `MonitorController.__init__`

```
if sys.platform == "win32":
    backend = WindowsDdcBackend.detect(config)   # raises/logs clearly if no DDC monitor found
else:
    backend = PowerDevilBackend.detect(config) if config.prefer_powerdevil else None
    backend = backend or DdcutilBackend(config)
```

- `PowerDevilBackend.detect` already degrades to `None` when `busctl` is missing, but gating on platform avoids a pointless subprocess attempt on Windows.
- `shows_native_osd` stays `False` on Windows — Windows shows no OSD for DDC/CI brightness changes, so Lunos's own notifications remain useful.
- Keep the existing startup log line pattern: `Brightness backend: Windows Monitor Configuration API (dxva2)`.

### 3. Notifications: PowerShell toast via subprocess

Keep the `notify()` seam shape (fire-and-forget subprocess, `check=False`) and branch on platform:

| Option | Verdict |
|---|---|
| **`powershell.exe` WinRT toast snippet** (chosen) | No new dependency, parallels `notify-send` exactly. One-liner invoking `Windows.UI.Notifications` with a text-only toast XML. Slow (~1s PowerShell startup) but notifications are rare and already fire-and-forget. |
| `windows-toasts` pip package | Nicer, maintained, but a Windows-only dependency. Revisit if the PowerShell path proves flaky. |
| `win10toast` | Unmaintained, known breakage on Win 11. No. |

`notification_timeout_ms` doesn't map 1:1 (toast duration is `short`/`long`) — map ≥10s to `long`, else `short`.

Two Windows-specific subprocess details, both easy to forget:

- **Console window flash**: the daemon runs under `pythonw.exe` (no console), so a
  plain `subprocess.run(["powershell", ...])` pops a visible console window for
  every notification. Pass `creationflags=subprocess.CREATE_NO_WINDOW` (plus
  `-NoProfile -NonInteractive -WindowStyle Hidden` on the PowerShell side) —
  `CREATE_NO_WINDOW` only exists on Windows, so build the kwargs conditionally.
- **AppUserModelID**: `ToastNotificationManager::CreateToastNotifier($AppId)`
  needs a registered AUMID or the toast is silently dropped. Use PowerShell's own
  well-known AUMID
  (`{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe`)
  — standard trick, no registration step needed. Toasts will show "Windows
  PowerShell" as the app name; acceptable for v1.

### 4. Service model: Scheduled Task at logon + `install.ps1`

Brightness control needs the user's desktop session (and the sensible default is
per-user anyway, matching the systemd *user* service), which rules out a classic
Windows service running as SYSTEM.

| Option | Verdict |
|---|---|
| **Task Scheduler logon task** (chosen) | Native, no dependency, supports restart-on-failure (via task XML settings), runs in user session, survives without an open console via `pythonw.exe`. |
| Startup-folder shortcut | Simplest but no restart-on-failure, no management CLI. |
| NSSM-wrapped service | Third-party binary to ship; session-0 isolation complicates desktop access. |
| pywin32 native service | New dependency + service boilerplate; session isolation again. |

`install.ps1` (parallel to `install.sh`, same idempotent spirit):

1. Locate Python via the `py -3` launcher; create/reuse `venv\`.
2. `venv\Scripts\pip install -r requirements.txt`.
3. Write a Task Scheduler task (via `Register-ScheduledTask` or an XML template + `schtasks`):
   - Trigger: at logon of current user.
   - Action: `venv\Scripts\pythonw.exe main.py` with stdout/stderr redirected to a log file (see logging below), working directory = repo.
   - Settings: restart on failure (e.g. 3 restarts, 1-min interval — mirrors `Restart=always`/`RestartSec=5` as closely as task XML allows), no execution time limit, run only when user logged on.
4. Start the task immediately.
5. Print status/log-viewing hints (`Get-ScheduledTask`, log file path) like `install.sh` does.

Idempotency details `install.sh` gets for free from `systemctl` that
`install.ps1` must do explicitly:

- **Stop the existing task before re-registering and restarting** — otherwise a
  re-run leaves the old process running and two Lunos instances race each other
  over DDC/CI (and fight via each other's `ManualOverrideGuard`).
- Overwrite the task definition unconditionally (`Register-ScheduledTask -Force`),
  mirroring how `install.sh` rewrites the unit file each run.
- Document that the script is run as
  `powershell -ExecutionPolicy Bypass -File install.ps1` (fresh machines default
  to `Restricted` and won't run `.ps1` files at all).

**Logging**: `pythonw.exe` has no console — `print` output vanishes. And unlike
Linux, there is no journald doing rotation: `run()` logs **one line per lux
reading** (sensor pushes roughly every second → ~5 MB/day), so a bare `>>`
redirect grows without bound. Plan:

- Add an optional `log_file_path: str | None = None` field to `Config`. When set,
  `log()` writes through a stdlib `logging.handlers.RotatingFileHandler` (e.g.
  2 × 5 MB) instead of `print`. No new dependency, no behavior change on Linux
  (field stays `None` there; journald keeps handling rotation).
- `install.ps1` points the task at a config with
  `log_file_path = %LOCALAPPDATA%\Lunos\lunos.log` and creates the directory.
  Since there's no external config file (per project model), the practical route:
  default `log_file_path` to the `%LOCALAPPDATA%` path *when running on Windows*
  and `None` elsewhere — keeps the no-config-file model intact.
- `flush=True` semantics carry over: the rotating handler flushes per record.

**Startup ordering**: the systemd unit has `After=network-online.target`; Task
Scheduler has no clean equivalent, and at logon Wi-Fi may not be up yet. No
mitigation needed — the reconnect loop (`reconnect_delay_seconds`) already
retries forever; just don't add a network precondition to the task (a failed
condition would prevent the task from ever starting).

**Restart semantics**: task-level restart-on-failure only fires on a *nonzero
process exit*. `run()`'s `while True` + blanket `except Exception` means the
process essentially never exits once it reaches the main loop — the task restart
setting is only a safety net for import-time/startup crashes (which the
no-hard-fail backend design above minimizes). This is weaker than
`Restart=always` but matches how the daemon actually fails in practice
(it doesn't — it retries internally).

Document the Windows equivalents in the README command table:
`Get-ScheduledTask Lunos`, `Get-Content -Wait $env:LOCALAPPDATA\Lunos\lunos.log`, `Start-/Stop-ScheduledTask Lunos`.

### 5. Packaging: script + venv now, executable later

Ship exactly like Linux: clone + `install.ps1`. A PyInstaller single-exe bundle is
explicitly **out of scope** for this feature (tracked separately if wanted — it
mainly matters once there's a tray app, see `system-tray-app.md`).

## Implementation steps

1. **`WindowsDdcBackend` in `main.py`** — ctypes structs (`PHYSICAL_MONITOR`), thin module-level dxva2 wrapper functions (with `use_last_error`), the backend class with normalization, handle caching, single re-enumeration retry on stale handles, and *lazy, non-fatal* enumeration (no startup crash when no monitor answers). All Windows API access lazy.
2. **Platform-aware selection** — `sys.platform` gate in `MonitorController.__init__`; confirm `shows_native_osd` and `supports_ramping` stay consistent (per CLAUDE.md gotcha).
3. **`notify()` Windows branch** — PowerShell toast subprocess with `CREATE_NO_WINDOW` + hidden/noninteractive flags and the PowerShell AUMID; keep `notify-send` path untouched on Linux.
4. **`log()` rotating-file option** — new `Config.log_file_path` (Windows default: `%LOCALAPPDATA%\Lunos\lunos.log`, `None` on Linux) backed by `logging.handlers.RotatingFileHandler`; `print` path unchanged when unset.
5. **Tests (`tests/test_main.py`)** — same fake-based style, no Windows needed:
   - Backend selection: monkeypatch `sys.platform` (or inject a platform param) and the detect functions; assert the right backend + `shows_native_osd` combination per platform.
   - `WindowsDdcBackend` percent math: fake the dxva2 wrapper functions; cover non-0–100 native ranges (e.g. min=0/max=254), min>0 ranges, and the failed-read → `None` path.
   - Stale-handle retry: fake a first-call failure, assert one re-enumeration then success.
   - Non-fatal startup: enumeration returning no monitors must not raise at construction; `get_current_pct()` → `None`, and a later successful enumeration recovers.
   - Description filter: `windows_monitor_description_contains` selects the matching monitor among several fakes.
   - `notify()` on Windows: assert the subprocess command is PowerShell-shaped and (on Windows) carries `CREATE_NO_WINDOW` (patch `subprocess.run`).
   - `log()` with `log_file_path` set writes via the rotating handler; unset keeps `print` behavior.
   - Run via `venv/bin/python3 -m unittest tests.test_main` after every change (project rule).
6. **`install.ps1`** — venv + task registration + log dir, idempotent re-runs (stop existing task, `Register-ScheduledTask -Force`, restart — like `install.sh` rewrites the unit file).
7. **Docs** — README: Windows install section (incl. `-ExecutionPolicy Bypass` invocation), command table (status/logs/restart equivalents), note on `lunarsensor.local` mDNS resolution and the IP-fallback in `Config.sensor_url`, warning about running other DDC/CI tools (Twinkle Tray etc.) alongside; new `Config` fields (`windows_monitor_description_contains`, `log_file_path`) added to the Configuration table. CLAUDE.md: Windows command equivalents + backend-consistency gotcha extended to the new backend.
8. **Manual validation on real Windows hardware** (cannot be unit-tested):
   - External monitor over HDMI/DP: read + write brightness, ramp visible.
   - Manual override: change brightness via monitor OSD buttons, confirm cooldown + offset adoption.
   - Sleep/resume and monitor unplug/replug: backend recovers via re-enumeration.
   - Logon with monitor still asleep / dock disconnected: daemon starts, logs the fallback, recovers when display appears.
   - Task auto-start at logon, restart after killing the process, log file fills and rotates.
   - Re-run `install.ps1` while running: exactly one instance afterwards.
   - Toast notification appears, no console window flash; sensor reachable via `lunarsensor.local` (else document IP fallback).

## Risks / known limitations

- **DDC/CI through docks/adapters is flaky** on some hardware (USB-C docks, DP-to-HDMI converters may not pass DDC). Same limitation ddcutil has; document, don't work around.
- **Third-party brightness tools race over DDC/CI.** Windows has no PowerDevil
  equivalent to arbitrate: if the user also runs Twinkle Tray, Monitorian, or
  similar, both programs write the same monitor. Interplay with
  `ManualOverrideGuard` is actually graceful — a Twinkle Tray change looks like a
  manual override, so Lunos backs off for the cooldown — but document that
  running both auto-adjusters at once is unsupported.
- **Monitor in DPMS sleep**: DDC calls fail while the display is off.
  `get_current_pct()` → `None` is already handled everywhere; a failed `set_pct`
  logs + notifies (same as on Linux). Repeated set failures while the monitor
  sleeps could spam toasts — acceptable for v1 since sets only fire on bucket
  *changes*, which need lux readings, which usually stop when the room's display
  is off; revisit only if it shows up in practice.
- **Multi-monitor**: first-working-monitor heuristic + description substring filter covers the common case; driving *multiple* monitors simultaneously stays out of scope (matches current Linux behavior, which also drives one display).
- **Handle invalidation after sleep** is the most likely field bug — the single re-enumeration retry plus the outer reconnect loop should absorb it, but this is the top item for manual validation.
- **PowerShell toast startup cost** (~1s per notification) — acceptable because notifications are rare and fire-and-forget; swap to `windows-toasts` only if it misbehaves.
- **No CI for Windows paths** — everything Windows-only is behind fakes in tests; real-hardware checklist (step 7) is the safety net.

## Out of scope

- PyInstaller / single-exe packaging.
- System tray UI (separate feature: `system-tray-app.md`).
- Driving internal laptop panels (WMI path) — Lunos targets external monitors.
- Simultaneous multi-monitor control.
