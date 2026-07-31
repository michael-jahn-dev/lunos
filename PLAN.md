# PLAN.md — Known issues from code review (2026-07-31)

Findings from a full review of `main.py`, `control.py`, `tray.py`, `install.sh`.
Assessment only — nothing fixed yet. Ordered by severity within each section.
Line numbers refer to the state of the tree at review time (commit `a49af54`).

## High priority

### 1. Hot reconnect loop on clean stream end
`main.py:1560-1578`, `read_ambient_lux_values()` (`main.py:1153`)

- `requests.get()` result is never checked with `raise_for_status()`. An HTTP
  404/500 response body is fed to `sseclient` as if it were an SSE stream:
  non-JSON lines are logged as `[sensor] ...` firmware output, then the stream
  ends **without an exception**.
- When the generator exhausts normally, the `for` loop in `run()` completes and
  the `while` loop reconnects **immediately** — `reconnect_delay_seconds` only
  applies on the exception path.
- Result: a misconfigured URL or a server that closes cleanly produces a tight
  reconnect loop (CPU burn + journal spam of "Connecting to sensor…").

**Fix:** call `response.raise_for_status()` before wrapping in `SSEClient`, and
treat normal stream EOF like the error path (apply the reconnect delay).
Add tests for both cases.

### 2. Failed/partial ramp is misread as a manual override
`main.py:1528-1540` (apply path), `MonitorController.ramp_to()` (`main.py:895`)

- If `ramp_to()` raises mid-staircase, the monitor is left at an intermediate
  step but `Daemon.current_pct` keeps the old value.
- The next `ManualOverrideGuard.poll_actual()` sees the divergence, and the
  loop records it via `record_override()`: a bogus **persistent** offset plus a
  5-minute cooldown — caused by a hardware hiccup, not the user.

**Fix:** after a ramp failure, re-anchor the tracked brightness (read the
actual value back, or track the last successfully written step inside
`ramp_to` and surface it to the caller).

### 3. No `timeout=` on any `subprocess.run`
`ddcutil` (`main.py:611`, `main.py:617`), `busctl` (`main.py:649`,
`main.py:665`), `notify-send` (`main.py:569`)

- `ddcutil` is known to hang on flaky DDC/CI buses; `busctl` blocks up to the
  D-Bus default timeout (25 s); `notify-send` can block on a hung notification
  daemon.
- A hung child freezes the loop thread forever. The process stays alive, so
  systemd's `Restart=always` never triggers. This is the most likely way the
  daemon silently dies in the field.

**Fix:** add a `timeout=` to every subprocess call; catch `TimeoutExpired` and
treat it as `get_current_pct() → None` / a failed set (RuntimeError), same as
a nonzero exit. Consider a systemd watchdog as a second line of defense
(see item 10).

## Medium priority

### 4. Command latency while sensor is connected but quiet
`main.py:1564-1571` (drain per event), `tray.py:678` (`_on_applied`)

- Commands are drained only once per SSE event. With the sensor connected but
  silent, `client.events()` blocks up to the read timeout (30 s) or the stale
  timeout (90 s). Tray commands (`restart`, `set_offset`, `set_config`) sit
  queued that long. The `Daemon` docstring promises "~1 lux reading".
- Related UX race: `SettingsDialog._on_applied` immediately re-fetches
  `get_schema`, which is answered inline from the **old** `self.config`
  (the loop hasn't drained the queued `set_config` yet) — the editors rebuild
  with stale values and the user's change looks lost.

**Fix ideas:** wake the loop on command arrival (e.g. read SSE in a helper
thread feeding a queue the loop `get(timeout=…)`s on), or at minimum have the
tray delay/refresh the schema rebuild from the push stream instead of an
immediate `get_schema`.

### 5. Backend reselect with unreadable monitor loses the anchor
`main.py:1388-1397` (`apply_config`)

- When `monitor.set_config()` re-selected the backend but the new backend's
  `get_current_pct()` returned `None`, `adopted_pct is None` skips re-anchoring
  entirely: `current_pct` still holds the old backend's value and the bucket
  index is stale. The next override poll can then fire a false manual override.

**Fix:** on backend reselect with an unreadable monitor, fall back to the
`anchor_to_monitor()` default-bucket branch instead of keeping stale state.

### 6. Traceback spam from short-lived control connections
`control.py:107` (banner send), `control.py:89` (`_is_live_socket` probe)

- `_send()` to a client that connected and immediately closed (exactly what the
  `_is_live_socket` probe does) raises `BrokenPipeError`; socketserver's
  `handle_error` prints a full traceback to the journal. The non-subscribe
  request path is equally unprotected.

**Fix:** wrap `handle()`'s send/reply cycle in `except OSError: return`
(mirroring what `_push_snapshots` already does).

### 7. Tray event socket half-broken states
`tray.py:154-157` (setup), `tray.py:196-207` (`_on_disconnected`)

- `_events` has no `disconnected`/`errorOccurred` handler:
  - `_event_buffer` is never cleared, so a partial line survives a reconnect
    and corrupts the first message (silently dropped by the JSON decode guard —
    first snapshot after reconnect is lost).
  - Availability is keyed only off the `_commands` socket: if the events socket
    dies alone, the UI shows "available" but never updates again.

**Fix:** mirror the `_commands` handlers on `_events` (clear buffer, feed
availability or at least force a reconnect of both sockets together).

### 8. NaN blinds the curve editor
`tray.py:384` (`rows()`), `tray.py:249-281` (`bucket_problems`),
`tray.py:297` (`paintEvent`)

- Non-numeric cell text becomes `float("nan")`. Every comparison in
  `bucket_problems` with NaN is `False`, so the row passes client-side
  validation silently.
- `CurvePreview.paintEvent` then computes `int(NaN)` → `ValueError` raised
  inside the paint handler, printed on **every repaint** while the text is
  invalid.
- The daemon rejects the value on Apply (`_coerce_buckets` checks
  `math.isfinite`), so no corruption — but noisy and confusing.

**Fix:** report NaN rows as errors in `bucket_problems`, and have
`CurvePreview` skip non-finite rows. Longer term: use a `QDoubleSpinBox`
delegate for the table (also fixes locale issue, see Minor).

## Daemon / systemd hygiene

### 9. Journal churn: one log line per lux reading
`main.py:1480` (`handle_reading`)

- ~1 line/second ≈ 86k lines/day into journald, forever, plus a snapshot
  publish per reading. Fine for debugging, wrong default for a daemon.

**Fix:** log on change only (bucket transition, override, error, backend
switch), or gate the per-reading line behind a verbose/debug config flag.

### 10. Unit file gaps
`install.sh:43-58`

- `ExecStart=$VENV_DIR/bin/python3 $PROJECT_DIR/main.py` is unquoted — a
  project path containing a space breaks the unit.
- `network-online.target` in the **user** manager is normally passive and
  unpopulated (NetworkManager-wait-online is system scope) — effectively a
  no-op. Harmless (the daemon reconnects anyway) but misleading.
- No watchdog: combined with item 3, a hung daemon is undetectable. Consider
  `Type=notify` + `WatchdogSec` with `sd_notify` pings from the loop, or at
  minimum document the limitation.

### 11. Shutdown order
`main.py:1592-1596` (`__main__` finally block)

- `server_close()` is called without `server.shutdown()` first: the
  serve_forever thread may still poll the closed socket. It's a daemon thread
  so exit works, but it can raise during interpreter teardown.

**Fix:** call `server.shutdown()` before `server.server_close()`.

## Minor

- **PowerDevil MaxBrightness cache never invalidated** (`main.py:703`): a
  monitor swap re-enumerated on the same D-Bus path (dock/undock) could scale
  against the wrong max. Edge case.
- **`float(lux)` on non-numeric sensor value** (`main.py:1200`): raises
  `ValueError`, caught upstream as "connection lost" — works, but the log
  message misdiagnoses the cause.
- **`with_suffix(".json.tmp")`** (`main.py:501`, `main.py:1087`): raises
  `ValueError` for odd user-supplied paths (e.g. filename ending in `.`).
- **Force-adopt race** (`main.py:1495`): a user manual change coinciding with
  PowerDevil appearing on D-Bus can be stomped once by the `_applied_write`
  push path. Documented trade-off, acceptable.
- **Locale in curve table** (`tray.py:383`): `float("0,5")` fails for users
  typing a decimal comma → NaN path (see item 8). A spin-box delegate fixes
  both.

## Explicitly reviewed and fine

- Threading model (loop thread owns all mutable state, queue for mutations,
  snapshot under lock) — sound; do not add locks around loop state.
- Server-side single-path validation (`coerce_config_value`) — correct.
- Atomic write-then-`os.replace` for config and offset state — correct.
- Overlapping-bucket hysteresis and the tray's inverted overlap warning —
  correct (see CLAUDE.md gotchas).
- Control socket permissions (0700 dir, 0600 socket) — correct.
- `control.py` importing nothing from `main.py` — correct, keep it that way.
