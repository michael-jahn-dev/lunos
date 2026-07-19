# Lunos

Ambient-light brightness daemon for Linux. Reads lux values from a
[Lunar](https://lunar.fyi)-compatible ESP32 ambient-light sensor's SSE stream and automatically
adjusts an external monitor's brightness — through KDE Plasma's PowerDevil when available, or
directly via DDC/CI (`ddcutil`) otherwise.

Not affiliated with or endorsed by Lunar / Alin Panaitiu. Lunos is an independent client that
talks to the sensor over its openly-documented [ESPHome](https://esphome.io) SSE API (see
[lunar.fyi/sensor](https://lunar.fyi/sensor)) — the sensor is commodity ESP32 hardware you flash
and own yourself.

## How it works

1. Connects to the sensor's SSE endpoint (`http://lunarsensor.local/events` by default).
2. Filters the stream for the ambient-light channel and ignores everything else, including the
   firmware's own log lines (e.g. sensor-saturation warnings), which are printed readably instead
   of being treated as errors.
3. Smooths raw readings with a moving median filter to suppress single-sample spikes.
4. Maps the smoothed lux value to a brightness percentage using an overlapping bucket table (see
   [Design notes](#design-notes)), which provides built-in hysteresis so minor light fluctuations
   don't cause flicker.
5. Applies brightness changes through whichever backend is available (see
   [Design notes](#design-notes)): a single instant call for a normal single-bucket change on
   PowerDevil, or that plus a short ramp of a few steps for a large jump on ddcutil (e.g. someone
   pointing a flashlight at the sensor).
6. If the sensor stream stays connected but stops producing valid readings for too long (e.g. it's
   saturated by direct light), the daemon logs why and forces a reconnect instead of sitting idle.
7. If it notices the monitor's actual brightness no longer matches what it last applied (e.g. you
   used the keyboard brightness keys), it treats that as a manual override: it stops auto-adjusting
   for a cooldown period and resumes from that new value afterwards, instead of immediately
   overriding you.
8. Runs as a systemd user service and auto-restarts on failure.

## Requirements

- Linux with `systemd --user` support
- Python 3.10+
- A monitor that supports DDC/CI (check with `ddcutil detect`), and one of:
  - KDE Plasma 6.0.4+ (uses PowerDevil's own DDC/CI brightness control automatically, no extra
    setup — needs `busctl`, which ships with `systemd` and is virtually always present), or
  - [`ddcutil`](https://www.ddcutil.com/) installed, for every other desktop (or no desktop)
- A Lunar-compatible ambient-light sensor (ESP32/ESP8266 + TSL2591) on the same network,
  reachable at `lunarsensor.local` — see [lunar.fyi/sensor](https://lunar.fyi/sensor) for
  flashing/pairing instructions
- `notify-send` (optional) for desktop notifications on brightness changes

## Installation

```sh
./install.sh
```

This creates a Python venv in `venv/`, installs the dependencies from `requirements.txt`, and
registers + starts a systemd user service (`lunos.service`) that restarts automatically.

## Usage

```sh
systemctl --user status lunos.service      # check it's running
journalctl --user -u lunos.service -f      # watch live logs
systemctl --user stop lunos.service        # stop it
systemctl --user disable lunos.service     # stop it from starting on login
```

## Configuration

There's no external config file — every setting lives in the `Config` dataclass at the top of
`main.py`. Re-run `install.sh` (or just restart the service) after changing it. Notable fields:

| Field | Purpose |
|---|---|
| `sensor_url` | SSE endpoint of the ambient-light sensor |
| `sensor_event_id` | SSE channel id to read lux values from — must match your sensor's actual firmware id (check with `curl -N lunarsensor.local/events`), which can differ from lunar.fyi's generic docs example |
| `monitor_display` | `ddcutil` display number, if you have more than one monitor (see `ddcutil detect`); only used by the ddcutil backend |
| `prefer_powerdevil` | Use KDE PowerDevil when available instead of ddcutil directly (see [Design notes](#design-notes)) |
| `powerdevil_display_label_contains` | Optional substring to pick a specific external display under PowerDevil; defaults to the first non-internal one |
| `powerdevil_show_osd` | Show Plasma's own brightness OSD for Lunos's automatic changes too (also what makes the brightness applet's slider stay in sync); only used by the PowerDevil backend. When enabled, the desktop notification on brightness change is skipped too, since the OSD already shows it |
| `buckets` | The lux-to-brightness bucket table (see [Design notes](#design-notes)); tune to your own room/monitor |
| `default_bucket_index` | Bucket to assume at cold boot if the monitor's current brightness can't be read |
| `median_window` | Raw samples used for outlier suppression |
| `max_transition_steps` / `transition_step_granularity_pct` | Ramp tuning for large brightness jumps |
| `transition_step_delay_seconds` | Pacing delay between individual ramp steps |
| `min_seconds_between_updates` | Rate limit between brightness changes |
| `stale_reading_timeout_seconds` | How long to tolerate a connected-but-silent sensor before reconnecting |
| `reconnect_delay_seconds` | Wait time before retrying a dropped/failed SSE connection |
| `connection_timeout_seconds` | Connect + read timeout for the SSE HTTP request |
| `override_poll_interval_seconds` / `manual_override_tolerance_pct` | How often, and how sensitively, to check for a manual brightness change |
| `manual_override_cooldown_seconds` | How long to pause auto-adjustment after a manual change is detected |
| `notifications_enabled` | Toggle desktop notifications |
| `notification_timeout_ms` | How long a desktop notification stays visible |

## Design notes

### Brightness backend: PowerDevil vs. ddcutil

On KDE Plasma 6, PowerDevil already manages external-monitor brightness over DDC/CI itself, and
exposes it over D-Bus as `org.kde.ScreenBrightness` (root object at `/org/kde/ScreenBrightness`,
listing per-display child objects at `/org/kde/ScreenBrightness/[name]` implementing
`org.kde.ScreenBrightness.Display`, with `Brightness`/`MaxBrightness`/`IsInternal`/`Label`
properties and a `SetBrightness(brightness, flags)` method — confirmed against KDE's own source in
the `powerdevil` repo, `daemon/dbus/org.kde.ScreenBrightness*.xml`). If Lunos called `ddcutil`
directly on such a system, two problems would show up: Plasma's own brightness slider/OSD would go
stale (it only reflects brightness changes it made itself), and two independent programs would be
writing to the same monitor over DDC/CI, exactly the kind of conflict Plasma 6.0.4 specifically
restructured its own DDC handling to avoid.

`MonitorController` picks a backend at startup: `PowerDevilBackend.detect()` looks for the
`org.kde.ScreenBrightness` service and the first non-internal display under it (or one matching
`powerdevil_display_label_contains`, if set); if found, brightness changes go through PowerDevil,
so Plasma's own UI/OSD is always accurate — it's the one making the change. Otherwise
(`prefer_powerdevil = False`, no Plasma, `busctl` missing, or no matching display), it falls back
to `DdcutilBackend`, calling `ddcutil` directly as before. D-Bus calls go through `busctl` (ships
with `systemd`, no extra dependency) rather than a Python D-Bus binding, to avoid adding a
dependency that's only needed on one of the two paths.

The two backends also apply brightness differently: `DdcutilBackend.supports_ramping` is `True`
(see ramped transitions below — raw ddcutil doesn't protect the monitor from rapid writes on its
own), while `PowerDevilBackend.supports_ramping` is `False`, since PowerDevil 6.0.4+ already
debounces and rate-limits its own DDC/CI writes to protect monitor lifespan — layering Lunos's ramp
on top would just be redundant latency. `MonitorController.ramp_to()` checks this flag and applies
the target in one call on PowerDevil, or steps it on ddcutil.

### The lux-to-brightness curve

The lux-to-brightness mapping is modeled on how real laptop ambient-light sensors behave
(specifically Windows 11's documented "bucketed ALR curve", and the general shape of macOS's
auto-brightness), rather than a hand-rolled smoothing curve.

### Overlapping bucket table instead of a continuous curve + threshold

Windows 11's default auto-brightness curve maps lux ranges to a small number of overlapping
buckets, each with a fixed target brightness percentage. The overlap is what prevents flicker: a
reading that's already inside the *current* bucket's range never changes the brightness, even if
it would fall into a different bucket when read cold. Lunos uses the same idea, scaled to this
project's sensor range (0–1000 lx) and monitor range (5–100%):

| Bucket | Min lux | Max lux | Target % |
|---|---|---|---|
| 1 | 0   | 10   | 5   |
| 2 | 5   | 50   | 20  |
| 3 | 15  | 100  | 35  |
| 4 | 60  | 300  | 50  |
| 5 | 150 | 400  | 65  |
| 6 | 250 | 650  | 80  |
| 7 | 350 | 1000 | 100 |

Selection rule: stay in the current bucket if it still contains the (median-filtered) reading;
otherwise move to whichever containing bucket is closest to the current one. Like Windows, Lunos
starts on bucket 2 at cold boot (the most common indoor lighting condition) until a real reading
arrives — though in practice it re-anchors to the monitor's actual current brightness first (see
below).

A moving median filter (window of 3 raw samples) still runs on the raw lux stream before bucket
selection, to swallow single-sample sensor spikes. That's a distinct concern from bucket
hysteresis — real ALS drivers do both: smooth the raw signal, then bucket the smoothed result.

### Ramped brightness transitions instead of a single jump

Real displays dim/brighten in visible steps, not a single instant jump (this is explicitly part of
Microsoft's own ALS conformance tests: "the screen brightness should smoothly transition up and
down"). `MonitorController.ramp_to()` steps from the last-applied percentage to the target via
repeated `ddcutil setvcp` calls.

Unlike a laptop's internal panel, where the backlight is PWM-driven and steps are essentially free,
each step here is a real DDC/CI round-trip over I2C — often a few hundred ms on real hardware. An
early version scaled step count to keep individual steps small (~3%), which meant large brightness
swings (e.g. a flashlight pointed straight at the sensor) could take 10+ seconds to finish ramping,
reading as the daemon being "slow" or stuck. Step count is now capped at `max_transition_steps`
(default 4) regardless of delta size: a normal single-bucket change (~15%, the bucket table's own
granularity) collapses to one instant call, and only genuinely large multi-bucket jumps get a
short, bounded staircase instead of either one jarring jump or a multi-second slideshow.

### Startup re-anchoring

On startup, Lunos reads the monitor's current brightness via `ddcutil getvcp 10` and anchors to
whichever bucket's target is closest to it — not a hardcoded default — then ramps from there.
Anchoring to a fixed bucket regardless of the real starting brightness was an earlier bug: if the
first few readings happened to fall in that assumed bucket's range, the mismatch between the real
brightness and the bucket's target would never get corrected, since nothing looked like a bucket
*change*. If the startup read fails entirely, it falls back to bucket 2's default target (20%).

### Manual override detection (macOS-style)

Windows immediately overrides manual brightness changes with the next auto-adjustment; macOS
instead detects a manual change and respects it for a while before resuming automatic control.
Lunos follows the macOS model: `ManualOverrideGuard` periodically (`override_poll_interval_seconds`)
polls the monitor's actual brightness and compares it to what the daemon last applied. A mismatch
beyond `manual_override_tolerance_pct` (e.g. you pressed a brightness key) is treated as a manual
override — automatic adjustment pauses for `manual_override_cooldown_seconds`, and the manually-set
value becomes the new tracked baseline rather than being discarded, so once the cooldown ends,
future adjustments are relative to where you left it, not a snap back to the old bucket target.

The mismatch also sets a standing `offset_pct` (the difference between your manually-set value and
the current bucket's raw table target), which every future automatic adjustment adds on top of its
own target, clamped to 0–100%. If you consistently nudge the brightness a bit brighter or dimmer
than what Lunos picks, later bucket changes track that preference instead of reverting to the bare
table values each time. The offset is replaced (not accumulated) by the next manual change, and
only lives for the current run of the daemon — it isn't saved to disk, so it resets on restart.

## License

MIT — see [LICENSE](LICENSE).
