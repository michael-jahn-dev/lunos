# System tray app (Fedora)

**Status:** Implemented (pending the manual-validation checklist at the end of this document)
**Tracking issue:** [#2](https://github.com/michael-jahn-dev/lunos/issues/2)

Implemented as designed, with one deviation worth recording: `run()` was refactored into a
`Daemon` class rather than kept as a function. The command queue, the state snapshot and a
single testable loop iteration all need somewhere to live, and threading them through a
function's locals would have been worse than the class. `run(config)` remains as a thin wrapper.

## Summary

A Linux (Fedora) system-tray application that puts Lunos's settings behind a GUI
instead of the hardcoded `Config` dataclass in `main.py`. It also exposes a live,
adjustable manual brightness offset — surfacing what `ManualOverrideGuard.offset_pct`
already does internally — so the user can nudge the auto-brightness up or down
without editing code. This removes today's edit-`main.py`-and-restart loop.

The daemon stays a headless, independently useful process. The tray app is a thin
client that talks to it over a per-user Unix socket, and the daemon keeps running
(with current behavior, no config file) when the app was never installed.

## Motivation

Configuration currently requires editing the `Config` dataclass and restarting the
service. A tray app makes settings and the manual offset adjustable at runtime by
anyone, not just someone comfortable editing Python.

## Target environment

Verified on the development machine, and the baseline this plan assumes:

| | |
|---|---|
| Distribution | Fedora 44 |
| Desktop | KDE Plasma 6, **Wayland** session |
| System Python | 3.14 |
| Tray protocol in use | `org.kde.StatusNotifierWatcher` (owned by `kded6`), host `org.kde.StatusNotifierHost-*` (plasmashell) |
| Toolkit available as RPM | `python3-pyside6` 6.11.1 (`dnf` repos) |
| Runtime dir | `/run/user/1000` (`$XDG_RUNTIME_DIR`) |

Two Fedora-specific consequences shape the whole design:

- **The tray is StatusNotifierItem (SNI) over D-Bus, not XEmbed.** The legacy X11
  system-tray protocol cannot work in a Wayland session at all. Anything that
  appears in the Plasma tray does so as an SNI item with its menu exported over
  DBusMenu (`com.canonical.dbusmenu`) — which constrains what the menu can contain
  (see [§3](#3-tray-item-sni-constraints-drive-the-menu-design)).
- **Fedora Workstation's default desktop is GNOME, which has no tray at all.**
  GNOME Shell needs `gnome-shell-extension-appindicator` before an SNI item is
  shown. Fedora KDE (this machine) works out of the box. The app must detect the
  missing watcher and say so rather than starting invisibly.

## What already exists, and what has to be built

An audit of `main.py` against what a tray app needs:

| Capability | Today | Needed |
|---|---|---|
| Tunables in one place | ✅ frozen `Config` dataclass | Reuse as the *schema*; add a file overlay |
| Manual offset value | ✅ `ManualOverrideGuard.offset_pct`, persisted to `offset_state_file` | Add a setter path (currently only derivable from a detected manual change) |
| Atomic state persistence | ✅ `_save_offset()` write-then-`os.replace` | Reuse the same pattern for the config file |
| Live daemon state (lux, bucket, brightness, backend) | ⚠️ computed in `run()`, only logged | Publish as a snapshot over IPC |
| IPC surface | ❌ none | New control socket + protocol |
| Runtime config change | ❌ restart-only, `Config` is frozen and captured at construction | `set_config()` on each component + an apply matrix |
| Backend/service status for a UI | ⚠️ `MonitorController.backend` type is known internally | Expose backend name, PowerDevil-pending flag, sensor connection state |
| Packaging | ✅ `install.sh` + `lunos.service` | Second unit `lunos-tray.service` + `.desktop` file |
| GUI | ❌ none | New `tray.py` |

So the work is: an IPC server in the daemon, a config-overlay file, runtime
re-application of settings, and a separate Qt client process.

## Design decisions

### 1. Process model: separate daemon and tray, tray is a pure client

The tray app is a **second process**, not a mode of `main.py`.

| Option | Verdict |
|---|---|
| **Separate processes, socket IPC** (chosen) | Brightness control survives the GUI crashing, logging out of a tray-less session, or the user quitting the applet. Keeps `lunos.service` exactly as it is. Matches the existing systemd *user* service model. |
| Single process, tray optional at startup | Removes IPC entirely, but couples brightness control to a GUI toolkit and to a graphical session; a Qt crash or `Quit` click kills auto-brightness. Also drags PySide6 into the daemon's dependency set. |
| Tray spawns/owns the daemon | Same coupling, plus a second lifecycle to manage on top of systemd's. |

Consequence worth stating up front: **the tray app imports nothing from `main.py`.**
It learns the config schema, defaults and current values over the wire
(`get_schema`, see [§4](#4-ipc-unix-socket--newline-delimited-json-stdlib-only)).
That keeps the daemon the single source of truth for what a setting *is*, and —
practically — means the tray can run on the **system interpreter** (`/usr/bin/python3`)
with the RPM-provided PySide6, while the daemon keeps running from its own `venv/`
with `requests`/`sseclient`. No shared interpreter, no `--system-site-packages`
surgery on the existing venv, no PySide6 in `requirements.txt`.

### 2. GUI toolkit: PySide6 (Qt 6) from the Fedora RPM

| Option | Verdict |
|---|---|
| **PySide6 + `QSystemTrayIcon`** (chosen) | Qt 6 is what Plasma 6 itself is built on — native look, correct Breeze theming and DPI handling. Qt's Unix backend already implements SNI + DBusMenu, so the tray "just works" on Wayland without hand-rolling D-Bus. Packaged as `python3-pyside6` (LGPL, no license question). |
| PyQt6 (`python3-pyqt6`) | Equivalent technically; GPL/commercial licensing is a worse fit for a project that currently has no such constraint. |
| GTK4/PyGObject + libayatana-appindicator | Non-native on Plasma; AppIndicator adds a C library for the one thing Qt does natively. |
| `pystray` / hand-rolled SNI over `busctl` | `busctl` can *call* D-Bus but cannot *serve* an object, so an SNI item can't be implemented the way `PowerDevilBackend` is. A hand-rolled implementation means writing StatusNotifierItem *and* DBusMenu from scratch — hundreds of lines of protocol for a tray icon. |

Install is `sudo dnf install python3-pyside6`, checked by `install.sh`
(see [§8](#8-packaging-second-systemd-user-unit--desktop-file)). Document
`pip install PySide6` into a separate venv as the non-Fedora fallback only.

Two Qt-on-Wayland details that are easy to lose a day to:

- **`QApplication.setDesktopFileName("dev.michaeljahn.Lunos")` is mandatory.** Under
  Wayland the compositor derives a window's icon and task-manager identity from the
  `app_id`, which Qt takes from this call — not from `setWindowIcon()`. Without it
  the settings window shows a generic placeholder in the Plasma task manager.
- **`QSystemTrayIcon.isSystemTrayAvailable()` must be checked before `show()`.** On a
  desktop with no SNI watcher (GNOME without the extension) the item is silently
  dropped. Fall back to a visible error: log it, and show a normal window explaining
  that `gnome-shell-extension-appindicator` is needed.

### 3. Tray item: SNI constraints drive the menu design

The Plasma tray renders the app's menu through **DBusMenu**, which serializes menu
*items* — labels, icons, checkboxes, radio groups, submenus, separators. It does
**not** carry arbitrary widgets. `QWidgetAction` (the obvious way to put a slider in
a menu) renders in Qt's own popup but is dropped by DBusMenu, so a slider embedded
in the tray menu would work in a test harness and be invisible in the real tray.

Menu layout that survives that constraint:

```
Lunos                                   (disabled item, heading)
342 lx · 65% · bucket 5 (80%)           (disabled item, live state)
Offset +10%                             (disabled item)
Backend: KDE PowerDevil                 (disabled item)
─────────────────────────────────────
Brightness offset          ▸  [radio group: -30 … +30 in 5% steps, current checked]
                              ─────
                              Reset to 0%
─────────────────────────────────────
☐ Pause auto-adjustment                (checkable; reflects override cooldown too)
─────────────────────────────────────
Settings…
Restart daemon
─────────────────────────────────────
Quit tray app                          (does NOT stop the daemon — label says so)
```

- The **offset radio submenu** is the DBusMenu-safe substitute for a slider. A real
  `QSlider` lives in the settings window, where it is a normal widget in a normal
  window and works fine.
- The **state header items** update from the daemon's push stream (§4), so the tray
  is live without polling. Plasma re-reads the menu on open; also refresh on
  `aboutToShow`. The summary is split across several disabled items rather than one
  item containing `\n`: DBusMenu carries a label, and a host may render an embedded
  newline as a space or clip the label at it. The tooltip is a single string, so it
  gets the same lines with real breaks.
- Both percentages are shown — the bucket's own target and the brightness actually
  applied. They differ by the offset and by `min_brightness_pct`, so showing only one
  makes a clamped or offset value look like a bug.
- **Tooltip** carries the same state, for hover without opening the menu.
- **Left click** triggers SNI `Activate` → open the settings window. Plasma's default.
- **Icon:** `QIcon.fromTheme()` with a fallback chain
  `video-display-brightness-symbolic` (Breeze, present on Fedora KDE) →
  `display-brightness-symbolic` (Adwaita) → a bundled SVG in `assets/`. SNI passes an
  icon *name* to the host when one is available, which is what lets the tray follow
  the user's icon theme and light/dark switch; a raw pixmap would not.
- **Status icon variants:** normal / paused (offset or cooldown active) / error
  (daemon unreachable or sensor disconnected). Use SNI `Status = NeedsAttention`
  only for the error case.

### 4. IPC: Unix socket + newline-delimited JSON, stdlib only

| Option | Verdict |
|---|---|
| **`AF_UNIX` socket, one JSON object per line** (chosen) | Zero new daemon dependencies — `socket`/`socketserver`/`json`/`threading` are stdlib. Directly testable with the project's existing fake-based unittest style. The tray side is `QLocalSocket`, which is already in PySide6 and integrates with the Qt event loop (no client thread). Debuggable by hand: `socat - UNIX-CONNECT:$XDG_RUNTIME_DIR/lunos/control.sock`. |
| D-Bus session service `dev.michaeljahn.Lunos` | The idiomatic desktop answer, and it would give `busctl` introspection, signals and systemd activation for free — but **`busctl` cannot serve an object**. Exporting one needs `python3-dasbus` + `python3-gobject-base` (both in Fedora repos, `dasbus` even installed here) plus a GLib main loop thread inside the daemon. That is exactly the dependency the project deliberately avoided for `PowerDevilBackend` ("via the `busctl` CLI — deliberate, to avoid a dependency needed on only one path"), and it would now be needed on *every* path. Revisit if a Lunos CLI or D-Bus activation is ever wanted; the command set below maps 1:1 onto a D-Bus interface. |
| Config file the daemon watches (inotify/poll) | One-directional: no live state readback, no "restart daemon", no immediate feedback in the UI. Also invites write races between the two processes. |

**Socket path:** `$XDG_RUNTIME_DIR/lunos/control.sock`, directory created `0700`.
`$XDG_RUNTIME_DIR` is per-user, `tmpfs`, and cleaned up by systemd at logout — so no
stale socket survives a reboot, and file permissions are the entire access control
story (the same trust boundary as the session bus). Fall back to
`~/.cache/lunos/control.sock` and unlink a stale socket at startup when
`XDG_RUNTIME_DIR` is unset (e.g. a bare `ssh` session).

The **subdirectory is deliberate**, not tidiness: Flatpak can share
`--filesystem=xdg-run/lunos` into a sandbox, but has no way to share a single socket
sitting directly at `$XDG_RUNTIME_DIR/lunos.sock`. Keeping the directory costs
nothing now and is the difference between a sandboxed tray being possible later or
not (see [§12](#12-packaging-formats-rpm-first-flatpak-not-yet)).

**Protocol.** Request/response, one compact JSON object per line, UTF-8:

| Command | Payload | Reply |
|---|---|---|
| `get_state` | — | live snapshot: `raw_lux`, `median_lux`, `bucket_index`, `bucket_pct`, `brightness_pct`, `offset_pct`, `override_active`, `override_seconds_left`, `backend`, `powerdevil_pending`, `sensor_connected`, `last_error` |
| `get_schema` | — | per-`Config` field: `name`, `type`, `default`, `current`, plus UI hints (`min`/`max`/`step`/`unit`) from a hand-maintained table |
| `set_config` | `{"fields": {...}}` | `{"ok": true, "applied": [...], "restart_required": [...]}` or `{"ok": false, "errors": {...}}` |
| `set_offset` | `{"offset_pct": int}` | ok + new state |
| `pause` / `resume` | optional `seconds` | ok + new state |
| `reload_config` | — | re-read `config.json` from disk (for hand edits) |
| `restart` | — | ok, then the daemon exits cleanly; `Restart=always` in the unit brings it back (see [§10](#10-service-control-from-the-tray)) |
| `subscribe` | — | connection switches to push mode: a state object per daemon loop iteration |

Every reply carries `{"ok": bool}` (errors add `"error"`) and `{"protocol": 1}`, so a
tray talking to a newer or older daemon can refuse cleanly instead of misparsing.
The server also writes one banner line on connect, which makes `socat` sessions
self-describing.

**Threading.** `run()`'s loop is synchronous and single-threaded, and all the mutable
state (`current_pct`, `current_bucket_index`, the guard, the filter) belongs to it.
The server therefore runs in a **daemon thread** and never touches that state
directly:

- **Inbound commands** go onto a `queue.Queue`; `run()` drains it at the top of each
  loop iteration and executes commands on the loop thread.
- **Outbound state** is a snapshot dict the loop publishes under a `threading.Lock`
  after each iteration; server threads only read the snapshot.

Latency is therefore one lux reading (~1 s) for a command to take effect — fine for a
settings UI, and worth stating in the docs. Two edge cases to handle explicitly:

- While the sensor is down, the loop sits in `time.sleep(reconnect_delay_seconds)`,
  so commands can be delayed by up to that long. Acceptable; the snapshot's
  `sensor_connected: false` lets the UI show *why* it feels unresponsive.
- The daemon must reply to `set_config` before the command is applied, or the tray
  blocks for a second on every keystroke. Reply "accepted" immediately after
  *validation* (which is synchronous and pure), and let the push stream confirm the
  effect.

### 5. Configuration: file overlay on top of the `Config` dataclass

The dataclass stays the schema, the defaults, and the documentation. A new file
stores **only explicit overrides**:

```
~/.config/lunos/config.json      # config: user intent, edited via the tray
~/.local/state/lunos/offset.json # state: the manual offset (already exists)
```

The config/state split is the existing one — `offset_state_file` is already
deliberately XDG *state*, not config, and stays there.

- `Config.load()` classmethod: read the JSON, drop unknown keys (with a log line, so
  a downgrade doesn't wipe them), type-check and range-check each known key, then
  `dataclasses.replace(Config(), **overrides)`. Any failure → log and fall back to
  pure defaults; the daemon must never fail to start because of a bad config file.
- `buckets` serialize as a list of `[min_lux, max_lux, brightness_pct]` triples and
  rebuild into `tuple[Bucket, ...]`.
- **The daemon is the only writer.** The tray never writes the file; it sends
  `set_config` and the daemon validates, applies, then persists atomically with the
  same write-then-`os.replace` pattern `_save_offset()` already uses. Single writer
  = no races, no inotify, no merge logic.
- Absent file ⇒ today's behavior exactly. This is the backwards-compatibility
  guarantee: an existing install picks up the new daemon with zero change.
- Hand-editing the file while the daemon runs needs `reload_config` (or a service
  restart) — document it.

CLAUDE.md's "no external config file" section must be rewritten for this: the model
becomes "dataclass is the schema and defaults; the file holds overrides only".

### 6. Applying settings at runtime: an explicit apply matrix

`Config` is frozen and each component captures it at construction
(`self._config = config`), so "settings are live" is not free. Add a small
`set_config(config: Config)` method to `MonitorController`, `LuxMedianFilter`,
`BrightnessUpdateGate` and `ManualOverrideGuard` — explicit, trivially unit-testable,
and it keeps `Config` frozen (a new instance is swapped in, never mutated). Poking
`_config` from outside is not acceptable.

| Field(s) | Apply |
|---|---|
| `min_brightness_pct`, `min_seconds_between_updates`, `transition_*`, `max_transition_steps`, `notification*`, `monitor_display`, `powerdevil_redetect_interval_seconds`, `override_poll_interval_seconds`, `manual_override_tolerance_pct`, `manual_override_cooldown_seconds` | **Hot** — swap the config object, next iteration uses it |
| `powerdevil_show_osd` | Hot, but must also recompute `MonitorController.shows_native_osd` (per the CLAUDE.md gotcha about keeping OSD/ramping flags consistent with the backend) |
| `median_window` | Rebuild `LuxMedianFilter`, preserving the newest samples that still fit |
| `buckets` | Swap, then re-anchor `current_bucket_index` with `nearest_bucket_index_for_pct()` — the old index may not exist in the new table |
| `offset_state_file` | Rebuild the guard's `_state_path`; keep the in-memory `offset_pct`, persist it to the new location |
| `prefer_powerdevil`, `powerdevil_display_label_contains` | Re-run backend selection. Reset `_powerdevil_pending` / `_applied_write` consistently, and re-anchor to the new backend's reported brightness (the existing `maybe_adopt_powerdevil` re-anchoring logic is the precedent) |
| `sensor_url`, `sensor_event_id`, `connection_timeout_seconds`, `stale_reading_timeout_seconds` | **Reconnect** — all four are bound inside the live `read_ambient_lux_values(config)` generator, which captured the *old* `Config` object at call time; swapping the daemon's config has no effect on a running stream. Set a flag, break out of the `for` on the next event, and let the outer `while True` reconnect. Report `reconnecting: true` (not `restart_required`) so the UI says "reconnecting…" rather than "restart needed" |
| `default_bucket_index` | **No runtime effect** — only read at startup, and only when the monitor's brightness cannot be read at all. Persist it, mark it "applies at next start" in the UI, and do not pretend otherwise |

Two forwarding details the implementation must not miss, both consequences of
`self._config = config` being copied into more than one object:

- `MonitorController.set_config()` has to forward to **the backend instance**, which
  keeps its own `_config`. `monitor_display` (ddcutil) and `powerdevil_show_osd`
  (PowerDevil) are read there, not on the controller — updating only the controller
  would make those two settings silently do nothing.
- Whichever component rebuilds must be rebuilt *before* `run()`'s next use of it in
  the same iteration, since commands are drained at the top of the loop (§4).

### 7. Offset control: a real setter, not a synthesized manual change

`ManualOverrideGuard.offset_pct` is currently only ever set by `record_override()`,
derived from a detected mismatch. The tray needs to set it directly. Add:

```python
def set_offset(self, offset_pct: int) -> None:   # clamp to (-99, 99), persist, no cooldown
def clear_override(self) -> None:                # end the cooldown early
```

Semantics decision: **an offset set from the tray is explicit intent, so it applies
immediately and clears any running override cooldown.** Rationale — the cooldown
exists to stop auto-adjust from fighting a change the user just made by hand; a user
who moves the slider in Lunos's own UI is asking Lunos to act, not to back off. The
main loop then re-applies `target_bucket_pct + offset_pct` on the next reading
without waiting out the remaining cooldown.

Do not implement this by faking a `record_override()` call: that would recompute the
offset from `actual - ambient_target`, i.e. round-trip the value through the monitor
and give a different number than the one the user picked.

Note that `run()` currently only applies brightness when the *bucket* changes
(`if target_bucket_index == current_bucket_index: continue`). An offset change with a
stable bucket must therefore also force one application — otherwise dragging the
slider in a room with steady light does nothing until the light changes. Track a
"target dirty" flag set by `set_offset`/`set_config` and check it alongside the
bucket comparison.

### 8. Packaging: second systemd user unit + `.desktop` file

| Option | Verdict |
|---|---|
| **`lunos-tray.service`, `WantedBy=graphical-session.target`** (chosen) | `graphical-session.target` is active in this session and is the standard hook for session-scoped user services on both Plasma 6 and GNOME (both are systemd-managed on Fedora). Gets `Restart=` and `journalctl --user -u lunos-tray.service` for free, exactly like the daemon. |
| XDG autostart `.desktop` in `~/.config/autostart/` | Works on every desktop including non-systemd sessions, but no restart-on-failure and no unified log. Document as the fallback for non-Fedora users. |
| Started by the daemon | Wrong direction — the daemon must not depend on a graphical session. |

Unit sketch (`After=`/`PartOf=graphical-session.target`, `Restart=on-failure`,
`ExecStart=/usr/bin/python3 $PROJECT_DIR/tray.py`). Deliberately **not**
`Requires=lunos.service`: the tray must start and show "daemon not running" rather
than failing to start, since telling the user the daemon is down is one of its jobs.

A `dev.michaeljahn.Lunos.desktop` (installed to `~/.local/share/applications/`) is
required, not cosmetic — it is what `setDesktopFileName()` refers to and what gives
the window its Wayland identity, icon and notification identity. `NoDisplay=false`
so Settings is launchable from the application menu too.

`install.sh` changes:

- Optional `--with-tray` flag; without it, behavior is byte-for-byte what it is now.
- Check `python3 -c "import PySide6"` against the **system** interpreter; on failure
  print `sudo dnf install python3-pyside6` and exit non-zero. Do not silently
  `pip install` a ~150 MB wheel.
- Write the unit + `.desktop`, `systemctl --user daemon-reload`,
  `enable --now lunos-tray.service`, and `restart` it — same idempotent
  rewrite-and-restart shape the script already uses for `lunos.service`.

**Single instance:** two tray icons is a visible bug. Bind a `QLocalServer` on
`$XDG_RUNTIME_DIR/lunos/tray.lock`; if the bind fails and the existing socket
answers, send `raise-window` to the running instance and exit 0.

**Late tray watcher.** At login the app may start before `kded6` owns
`org.kde.StatusNotifierWatcher` — structurally the same race the daemon already
handles for PowerDevil (`maybe_adopt_powerdevil`). Qt's SNI backend watches for the
service and registers when it appears, but do not rely on it blindly: verify on real
login, and if the icon is missing, watch the name with `QDBusServiceWatcher` and
re-`show()`. This is a manual-validation item, not something a unit test can cover.

### 9. Settings window

A `QDialog` (not a `QMainWindow` — no toolbar or status bar to justify it), tabbed,
built from the `get_schema` reply so a new `Config` field needs no GUI code beyond a
line in the UI-hints table:

- **Sensor** — `sensor_url`, `sensor_event_id`, timeouts. A "Test connection" button
  that reports what the daemon's snapshot says (`sensor_connected`, `last_error`),
  plus a hint about `curl -N lunarsensor.local/events` for finding the real event id
  (the documented gotcha — the firmware id differs from lunar.fyi's docs example).
- **Curve** — a `QTableWidget` of buckets with add/remove/reset-to-defaults, a live
  plot of the lux→brightness curve, and validation: `min_lux < max_lux`,
  `1 ≤ brightness_pct ≤ 100`, ascending brightness. **Overlap between adjacent
  buckets is warned about when *missing*, not when present** — the overlap is the
  hysteresis, and a table with none will visibly flicker. Getting this warning
  backwards would be the single most damaging UI bug in this feature.
- **Behaviour** — offset slider, override tolerance/cooldown, update rate limit,
  ramp tuning, `min_brightness_pct` (with a note that it exists to stop a large
  negative offset from blacking the display out).
- **Backend** — `prefer_powerdevil`, display label filter, `monitor_display`,
  `powerdevil_show_osd`, and read-only status: active backend, whether PowerDevil is
  still pending, and the ddcutil/i2c situation on a non-Plasma system.
- **Notifications** — enable toggle and timeout, with the existing behavior spelled
  out: when PowerDevil shows its own OSD, Lunos suppresses its notification.

Fields whose validation fails are rejected by the daemon and reported inline; the
client validates too, but the daemon's check is the authoritative one (see §11).

### 10. Service control from the tray

Split by what each action actually needs:

- **Restart** goes over the control socket (`{"cmd": "restart"}`), not `systemctl`.
  The unit already has `Restart=always`, so a clean `sys.exit(0)` is a restart.
  This has nothing to do with elegance: it is the only form that survives a
  sandboxed tray (§12), and it removes a subprocess from the common path.
- **Status** comes from the socket too — if the socket answers, the daemon is up;
  the snapshot already carries backend and sensor state, which is more than
  `is-active` would tell us.
- **Start a stopped daemon** is the one case with no socket to talk to, so it shells
  out to `systemctl --user start lunos.service`. Same subprocess-a-system-CLI posture
  the project already takes with `busctl` and `ddcutil`, and it needs no privileges
  for a user unit. Degrade gracefully when `systemctl` is unavailable: show the
  command instead of a dead button.

`Quit tray app` stops only the GUI and must be labelled so; stopping the daemon is a
separate, explicitly labelled action.

### 11. Input validation is a daemon responsibility

The socket is only as trustworthy as its file permissions, and the daemon writes what
it receives to disk and pushes it to the monitor. Rules, all enforced server-side:

- **Whitelist field names** against `dataclasses.fields(Config)`. Never `setattr` an
  arbitrary key, never `eval`/`exec` a value, never accept a callable.
- **Type- and range-check every value** before `dataclasses.replace`: percentages
  clamp to their documented ranges, intervals must be positive and finite (a `0`
  poll interval turns the loop into a `ddcutil` spin), `median_window ≥ 1`,
  `sensor_url` must parse as `http`/`https`.
- **Cap the read**: a line-oriented protocol with an unbounded `readline()` is a
  memory exhaustion bug. Enforce a maximum message size and drop the connection past
  it.
- Reject unknown commands with an error reply rather than closing silently.
- Keep the socket directory `0700`; do not fall back to a world-writable location.

### 12. Packaging formats: RPM first, Flatpak not yet

For this feature, distribution stays what it is today — clone the repo, run
`install.sh --with-tray`. But the question of a "proper" package is worth deciding
now, because two of the decisions above were made *because* of the answer.

| Option | Verdict |
|---|---|
| **Git clone + `install.sh`** (chosen for this feature) | Zero new machinery, matches how the daemon is installed today, and the tray's only real dependency is one `dnf install`. |
| **RPM in a COPR** (the right next step, separate feature) | Ships *both* halves in one artifact, can `Requires: python3-pyside6, ddcutil`, drops both user units and the `.desktop` into place, and updates via `dnf upgrade`. Fedora-native, no sandbox to fight, and it is the only format that can package the daemon at all. |
| **Flatpak** | **Rejected for now** — see below. Keep it possible, don't build it. |

**Why Flatpak does not fit, specifically.** The objection is not "sandboxing is
hard"; it is that Flatpak can only wrap the *tray*, and a tray without the daemon is
an icon that does nothing:

- The daemon shells out to `ddcutil` (needs `/dev/i2c-*`), `busctl` (to reach
  `org.kde.ScreenBrightness`), and `notify-send` — and runs as a systemd user
  service. Every one of those is a sandbox hole or a portal rewrite, and the
  shell-out-to-the-system-CLI approach is a deliberate project decision
  (see CLAUDE.md), not an accident to be refactored away for packaging's sake.
- So a Flatpak tray means **two install mechanisms**: `flatpak install` for the
  applet, plus a git clone for the part that actually changes brightness. That is a
  worse install story than today's one command, not a better one.
- The usual Flatpak wins are weak here. Dependency isolation buys little when the
  dependency is one distro package that a KDE system has most of already; Flathub
  distribution presumes a release cadence, AppStream metainfo and a network-free
  build manifest that this project does not have yet; and the sandbox's security
  value is low for code the user cloned themselves.

**What was kept cheap so it stays possible.** Two decisions above were shaped by
this, at no cost today:

- The control socket lives in a **subdirectory** (`$XDG_RUNTIME_DIR/lunos/`), which
  `--filesystem=xdg-run/lunos` can share into a sandbox. A bare
  `$XDG_RUNTIME_DIR/lunos.sock` could not be shared and would have made a sandboxed
  tray impossible (§4).
- **Restart goes over the socket, not `systemctl`** (§10). A sandboxed app has no
  host systemd and no `systemctl` binary, so a tray built on `systemctl restart`
  would have had to be rewritten. Only "start a stopped daemon" still needs
  `systemctl`, and that one action degrading to a printed command is acceptable.

The remaining sandbox work, if it is ever picked up: SNI works unchanged (session
bus, Qt registers a unique name), the settings window needs nothing special, and the
manifest would need `--socket=wayland --socket=session-bus --filesystem=xdg-run/lunos
--filesystem=xdg-config/lunos`. That is a small manifest — the blocker was never the
tray, it is the daemon.

## Implementation steps

1. **Config overlay** — `Config.load()` / serialization / validation, bucket
   (de)serialization, atomic save reusing the `_save_offset()` pattern. Daemon
   behavior unchanged when the file is absent.
2. **Runtime re-application** — `set_config()` on `MonitorController`,
   `LuxMedianFilter`, `BrightnessUpdateGate`, `ManualOverrideGuard`; the apply matrix
   from §6 in `run()`, including bucket re-anchoring, backend re-selection and the
   reconnect flag.
3. **Offset setter** — `ManualOverrideGuard.set_offset()` / `clear_override()`, plus
   the "target dirty" flag in `run()` so an offset change applies without waiting for
   a bucket change.
4. **State snapshot** — a `snapshot()` producing the `get_state` dict, published
   under a lock at the end of each loop iteration; extend it with
   `sensor_connected` / `last_error` from the reconnect handler.
5. **Control server** — `AF_UNIX` server in a daemon thread, command queue, push
   subscriptions, size caps, socket path handling and stale-socket cleanup. New
   module (`control.py`) rather than growing `main.py`, since the tray does not
   import it and the daemon can run without it if the socket cannot be created.
6. **Tests (`tests/`)** — stdlib `unittest`, no GUI, no hardware, matching the
   existing fake-based style:
   - Config overlay: unknown keys dropped, bad types rejected, out-of-range clamped,
     corrupt/missing file → defaults, round-trip save/load, bucket triples.
   - Apply matrix: each hot field visible on the next iteration; `median_window`
     rebuild preserves samples; `buckets` change re-anchors the index;
     `powerdevil_show_osd` keeps `shows_native_osd` consistent; sensor fields set the
     reconnect flag.
   - Offset setter: clamping, persistence, cooldown cleared, and that it does *not*
     go through `record_override` (no monitor read involved).
   - Protocol: real socket in a `tmp_path`, one request per command, malformed JSON,
     oversized line, unknown command, unknown config field, `subscribe` push
     ordering.
   - Concurrency: commands enqueued from another thread are applied on the loop
     thread; snapshot reads never observe a half-updated state.
   - Run `venv/bin/python3 -m unittest discover -s tests -t .` after every change
     (project rule; `-t .` keeps `import main` working from the repo root).
7. **`tray.py`** — Qt application, SNI tray icon with the DBusMenu-safe menu,
   `QLocalSocket` client with auto-reconnect and a "daemon not running" state,
   settings dialog generated from `get_schema`, single-instance lock,
   `setDesktopFileName`, tray-unavailable fallback message.
8. **Packaging** — `lunos-tray.service`, `dev.michaeljahn.Lunos.desktop`, bundled
   fallback icon, `install.sh --with-tray` with the PySide6 RPM check.
9. **Docs** — README: tray section (install, screenshot, GNOME extension caveat),
   config-file location and precedence, socket path, new commands table entries.
   CLAUDE.md: **rewrite the "Configuration model" section** (no longer restart-only),
   correct "single-file daemon" (now `main.py` + `control.py` + `tray.py`), add the
   test-discovery command, and add a gotcha for the DBusMenu widget limitation and
   the daemon-owns-the-config-file rule.
10. **Manual validation** (cannot be unit-tested):
    - Fedora KDE Wayland: icon appears in the tray at login, menu opens, state
      updates live, offset submenu changes brightness within ~1 s.
    - Cold login race: tray started before `kded6` owns the watcher — icon still
      appears.
    - Daemon stopped: tray shows "not running", "Start daemon" works, tray recovers
      when the socket reappears.
    - Settings survive a daemon restart; hand-edited `config.json` + `reload_config`.
    - Backend switch at runtime (`prefer_powerdevil` off/on) re-anchors brightness
      instead of jumping.
    - Offset slider vs. monitor OSD buttons: no fight, cooldown behaves.
    - Icon follows a Breeze light/dark theme switch.
    - Fedora Workstation (GNOME) with and without
      `gnome-shell-extension-appindicator`: works, or fails with a clear message.
    - Second `tray.py` launch raises the existing window instead of adding an icon.

## Risks / known limitations

- **DBusMenu has no widgets.** Any menu design involving a slider, a spinbox or a
  live chart *in the tray menu* is not implementable; it works in a local Qt popup
  and silently disappears in the real tray. The radio-submenu design above is the
  workaround. This is the most likely source of "works on my machine" rework.
- **GNOME has no tray.** Fedora Workstation's default desktop needs an extension.
  Detected and reported, not worked around.
- **~1 s command latency**, and up to `reconnect_delay_seconds` while the sensor is
  down, because commands are applied on the loop thread. A UI that feels laggy is the
  cost of not introducing locks around the loop's state. Revisit only if it
  actually annoys.
- **No authentication on the socket** beyond `0700` file permissions — same trust
  boundary as the session bus, but worth stating: anything running as the user can
  drive the monitor and rewrite the config.
- **Config-file schema drift.** A field renamed in `Config` orphans the key in
  `config.json`. Unknown keys are dropped with a log line rather than migrated; if
  renames become common, add a `"version"` field and a migration step.
- **Two writers of brightness remain possible** — the tray does not change the
  existing situation where PowerDevil, `ddcutil` and Lunos can all address the same
  monitor. It only makes it visible (backend readout).
- **PySide6 is a large dependency** (RPM pulls a substantial Qt 6 stack). Acceptable
  on a KDE system where Qt 6 is already installed for the desktop itself; noticeable
  on a minimal or GNOME install. This is why the tray is optional and the daemon
  never imports it.
- **Qt/Plasma version skew** — the SNI/DBusMenu path is well-trodden, but tray
  behavior differs across Plasma point releases and GNOME extension versions. The
  manual checklist is the only real safety net; there is no CI for it.

## Out of scope

- Sensor firmware flashing/provisioning — separate feature
  ([`sensor-firmware-provisioning.md`](sensor-firmware-provisioning.md), [#3](https://github.com/michael-jahn-dev/lunos/issues/3)),
  which depends on this one.
- Windows tray support — [`windows-support.md`](windows-support.md) ([#1](https://github.com/michael-jahn-dev/lunos/issues/1))
  covers the daemon only; a Windows tray would need a different toolkit path.
- RPM/COPR and Flatpak packaging — decided against for this feature and reasoned
  through in [§12](#12-packaging-formats-rpm-first-flatpak-not-yet); an RPM is the
  recommended follow-up. Install stays clone + `install.sh`.
- A command-line client for the control socket. The protocol is deliberately
  hand-drivable with `socat`; a real CLI is a separate feature (and the point at
  which switching to D-Bus should be reconsidered).
- Multi-monitor control. Unchanged from today: Lunos drives one display.
- Multi-user / system-wide daemon. Everything stays a systemd *user* service.
