#!/usr/bin/env python3
"""
Lunos - Ambient Light Brightness Daemon

Reads lux values from a Lunar-compatible ESP32 ambient-light sensor
(SSE stream) and automatically adjusts an external monitor's brightness
via DDC/CI (through ddcutil).
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Protocol


import requests
import sseclient


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

class Bucket(NamedTuple):
    """One rung of the lux -> brightness curve. A tuple, so existing indexing still
    works, but named access (`.brightness_pct`) reads far better than `[2]`."""
    min_lux: float
    max_lux: float
    brightness_pct: int


@dataclass(frozen=True)
class Config:
    sensor_url: str = "http://lunarsensor.local/events"  # SSE endpoint the sensor's firmware exposes
    sensor_event_id: str = "sensor-ambient_light"  # only this SSE channel carries real lux values — matches this device's actual firmware id, not lunar.fyi's generic docs example

    monitor_display: str | None = None  # e.g. "1" if multiple monitors are addressed via ddcutil

    # On KDE Plasma 6, PowerDevil itself already drives external-monitor brightness over
    # DDC/CI (org.kde.ScreenBrightness D-Bus service). When available, Lunos prefers it over
    # calling ddcutil directly, so Plasma's own brightness slider/OSD stays in sync and two
    # programs don't race to write the same monitor over DDC/CI. Falls back to ddcutil
    # wherever that service isn't present (other desktops, no desktop at all, etc.).
    prefer_powerdevil: bool = True
    powerdevil_display_label_contains: str | None = None  # optional substring to pick a specific external display; defaults to the first non-internal one
    powerdevil_show_osd: bool = True  # show Plasma's own brightness OSD for Lunos's changes too; also appears to be
                                       # what makes the brightness applet's slider refresh, since PowerDevil's
                                       # Brightness D-Bus property has no EmitsChangedSignal annotation

    # At login, systemd may start Lunos before PowerDevil has registered org.kde.ScreenBrightness
    # (or before it has enumerated DDC/CI displays), so the startup detection can miss it and fall
    # back to ddcutil for the whole run - leaving Plasma's brightness cache out of sync with what
    # Lunos writes (a later manual +5% key press then jumps from Plasma's stale value). While on
    # the ddcutil fallback, Lunos re-checks for PowerDevil this often and switches over when it
    # appears.
    powerdevil_redetect_interval_seconds: float = 30.0

    median_window: int = 3          # number of raw samples in the moving-median filter (swallows single spikes)

    # Ramp tuning: each step is a real ddcutil round-trip over DDC/CI (slow, often
    # a few hundred ms on real hardware - unlike a laptop's near-instant PWM backlight),
    # so step count is capped regardless of how big the brightness delta is.
    transition_step_granularity_pct: int = 15  # ideal brightness change per step
    max_transition_steps: int = 4              # hard cap on steps per ramp, bounds worst-case latency
    transition_step_delay_seconds: float = 0.05  # extra pacing delay between steps

    min_seconds_between_updates: float = 2.0  # minimum gap between two applied brightness changes

    # Never drive the monitor below this, even after a remembered manual offset is applied.
    # A large negative offset (e.g. a -30% manual nudge while the screen was bright) added to
    # a low bucket target can otherwise clamp all the way to 0% and black the display out.
    min_brightness_pct: int = 5

    # Manual-override detection (mirrors macOS: a manual brightness change is respected
    # for a while instead of being immediately overridden by the next auto-adjustment).
    override_poll_interval_seconds: float = 3.0    # how often to check actual vs. tracked brightness
    manual_override_tolerance_pct: int = 3          # mismatch beyond this counts as a manual change
    manual_override_cooldown_seconds: float = 300.0  # how long to pause auto-adjustment afterwards

    # Where the manual-brightness offset survives restarts (state, not config - hence not a
    # config file but an XDG state file). None disables persistence: the offset then resets
    # to 0 on every restart, as it did before.
    offset_state_file: str | None = "~/.local/state/lunos/offset.json"

    # If the SSE connection stays open but no valid lux reading arrives for this long
    # (e.g. the sensor is saturated by direct light and stops publishing readings),
    # force a reconnect instead of sitting idle indefinitely.
    stale_reading_timeout_seconds: float = 90.0

    reconnect_delay_seconds: float = 5.0        # wait time before retrying a dropped/failed SSE connection
    connection_timeout_seconds: float = 30.0    # connect + read timeout for the SSE HTTP request

    notifications_enabled: bool = True   # show a desktop notification (via notify-send) on brightness changes
    notification_timeout_ms: int = 10000  # how long a desktop notification stays visible

    # Overlapping (min_lux, max_lux, brightness_pct) buckets mapping ambient light to a target
    # brightness. The overlap is intentional: it's what gives hysteresis "for free", the same way
    # Windows 11's default ambient light response curve avoids flicker without a separately tuned
    # threshold. Defaults are scaled to this project's sensor range (0-1000 lx) and monitor range
    # (5-100%) - tune to your own room/monitor if the defaults feel off.
    buckets: tuple[Bucket, ...] = (
        Bucket(0, 10, 5),
        Bucket(5, 50, 20),
        Bucket(15, 100, 35),
        Bucket(60, 300, 50),
        Bucket(150, 400, 65),
        Bucket(250, 650, 80),
        Bucket(350, 1000, 100),
    )
    default_bucket_index: int = 1  # bucket 2: the most common indoor lighting condition, same default as Windows


# --------------------------------------------------------------------------- #
# Bucketed lux -> brightness curve (modeled on Windows' bucketed ALR curve)
# --------------------------------------------------------------------------- #

def nearest_bucket_index_for_pct(buckets: tuple[Bucket, ...], pct: int) -> int:
    """Finds the bucket whose target percentage is closest to a given brightness."""
    return min(range(len(buckets)), key=lambda i: abs(buckets[i].brightness_pct - pct))


def select_bucket_index(buckets: tuple[Bucket, ...], lux: float, current_index: int) -> int:
    """
    Picks the bucket for a lux reading: stays in the current bucket if it still
    contains the reading (this is the hysteresis), otherwise moves to the
    containing bucket closest to the current one.
    """
    containing = [i for i, b in enumerate(buckets) if b.min_lux <= lux <= b.max_lux]
    if not containing:
        return 0 if lux < buckets[0].min_lux else len(buckets) - 1
    if current_index in containing:
        return current_index
    return min(containing, key=lambda i: abs(i - current_index))


# --------------------------------------------------------------------------- #
# Logging / notifications
# --------------------------------------------------------------------------- #

def log(message: str) -> None:
    print(message, flush=True)


def notify(message: str, config: Config) -> None:
    if not config.notifications_enabled:
        return
    subprocess.run(
        ["notify-send", "-t", str(config.notification_timeout_ms), "-i", "info", "-a", "Lunos", "Lunos", message],
        check=False,
    )


# --------------------------------------------------------------------------- #
# Monitor control backends
# --------------------------------------------------------------------------- #

class BrightnessBackend(Protocol):
    """The contract every backend implements, so MonitorController can treat them
    interchangeably. `supports_ramping` decides whether Lunos animates the change
    itself or hands over a single write (see MonitorController.ramp_to)."""

    supports_ramping: bool

    def get_current_pct(self) -> int | None: ...
    def set_pct(self, pct: int) -> None: ...


class DdcutilBackend:
    """Drives brightness directly via ddcutil (DDC/CI). Works everywhere ddcutil does."""

    supports_ramping = True  # raw ddcutil doesn't debounce/animate on its own, so Lunos ramps it

    VCP_BRIGHTNESS_CODE = "10"

    def __init__(self, config: Config):
        self._config = config

    def _display_args(self) -> list[str]:
        return ["--display", self._config.monitor_display] if self._config.monitor_display else []

    def set_pct(self, pct: int) -> None:
        command = ["ddcutil", "setvcp", self.VCP_BRIGHTNESS_CODE, str(pct)] + self._display_args()
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())

    def get_current_pct(self) -> int | None:
        command = ["ddcutil", "getvcp", self.VCP_BRIGHTNESS_CODE, "--brief"] + self._display_args()
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            return None

        tokens = result.stdout.split()
        try:
            vcp_index = tokens.index("VCP")
            current_value = int(tokens[vcp_index + 3])
            max_value = int(tokens[vcp_index + 4])
        except (ValueError, IndexError):
            return None

        if max_value <= 0:
            return None
        return round(current_value / max_value * 100)


# KDE Plasma 6's PowerDevil exposes external-monitor (DDC/CI) brightness control over D-Bus.
# Interface confirmed against KDE's own source (daemon/dbus/org.kde.ScreenBrightness*.xml in
# the powerdevil repo): a root org.kde.ScreenBrightness object lists per-display D-Bus names,
# each exposed as a child org.kde.ScreenBrightness.Display object at
# /org/kde/ScreenBrightness/[name] with Brightness/MaxBrightness/IsInternal/Label properties
# and a SetBrightness(brightness, flags) method.
POWERDEVIL_SERVICE = "org.kde.ScreenBrightness"
POWERDEVIL_ROOT_PATH = "/org/kde/ScreenBrightness"
POWERDEVIL_ROOT_INTERFACE = "org.kde.ScreenBrightness"
POWERDEVIL_DISPLAY_INTERFACE = "org.kde.ScreenBrightness.Display"
POWERDEVIL_SUPPRESS_INDICATOR_BIT = 1  # the SetBrightness "flags" bit value defined by KDE's D-Bus API itself


def _busctl_get_property(service: str, obj_path: str, interface: str, prop: str):
    try:
        result = subprocess.run(
            ["busctl", "--user", "-j", "get-property", service, obj_path, interface, prop],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)["data"]
    except (json.JSONDecodeError, KeyError):
        return None


def _busctl_call(service: str, obj_path: str, interface: str, method: str, signature: str, *args) -> bool:
    try:
        result = subprocess.run(
            ["busctl", "--user", "call", service, obj_path, interface, method, signature]
            + [str(arg) for arg in args],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


class PowerDevilBackend:
    """
    Drives brightness through KDE Plasma's PowerDevil instead of calling ddcutil directly.
    PowerDevil is itself the one talking DDC/CI to the monitor on Plasma 6, so going through
    it keeps Plasma's own brightness slider/OSD in sync and avoids two independent programs
    racing to write the same monitor over DDC/CI. PowerDevil already debounces/protects its
    own DDC/CI writes (a deliberate Plasma 6 change to avoid shortening monitor lifespan), so
    this backend applies brightness in a single call instead of Lunos's own ramp.
    """

    supports_ramping = False

    def __init__(self, display_path: str, config: Config):
        self._display_path = display_path
        self._config = config
        self._cached_max_brightness: int | None = None

    def _display_property(self, prop: str):
        return _busctl_get_property(
            POWERDEVIL_SERVICE, self._display_path, POWERDEVIL_DISPLAY_INTERFACE, prop
        )

    def _max_brightness(self) -> int | None:
        # MaxBrightness is a fixed property of the display, so read it once and reuse it -
        # avoids a second busctl process on every brightness poll (every few seconds).
        if not self._cached_max_brightness:
            self._cached_max_brightness = self._display_property("MaxBrightness")
        return self._cached_max_brightness

    @staticmethod
    def detect(config: Config) -> "PowerDevilBackend | None":
        names = _busctl_get_property(
            POWERDEVIL_SERVICE, POWERDEVIL_ROOT_PATH, POWERDEVIL_ROOT_INTERFACE, "DisplaysDBusNames"
        )
        if not names:
            return None

        for name in names:
            display_path = f"{POWERDEVIL_ROOT_PATH}/{name}"
            is_internal = _busctl_get_property(
                POWERDEVIL_SERVICE, display_path, POWERDEVIL_DISPLAY_INTERFACE, "IsInternal"
            )
            if is_internal is None or is_internal:
                continue

            if config.powerdevil_display_label_contains:
                label = _busctl_get_property(
                    POWERDEVIL_SERVICE, display_path, POWERDEVIL_DISPLAY_INTERFACE, "Label"
                ) or ""
                if config.powerdevil_display_label_contains.lower() not in label.lower():
                    continue

            return PowerDevilBackend(display_path, config)

        return None

    def get_current_pct(self) -> int | None:
        brightness = self._display_property("Brightness")
        max_brightness = self._max_brightness()
        if brightness is None or not max_brightness:
            return None
        return round(brightness / max_brightness * 100)

    def set_pct(self, pct: int) -> None:
        max_brightness = self._max_brightness()
        if not max_brightness:
            raise RuntimeError("Could not read MaxBrightness from PowerDevil")

        native_value = round(pct / 100 * max_brightness)
        flags = 0 if self._config.powerdevil_show_osd else POWERDEVIL_SUPPRESS_INDICATOR_BIT
        ok = _busctl_call(
            POWERDEVIL_SERVICE, self._display_path, POWERDEVIL_DISPLAY_INTERFACE,
            "SetBrightness", "iu", native_value, flags,
        )
        if not ok:
            raise RuntimeError("busctl SetBrightness call to PowerDevil failed")


class MonitorController:
    """
    Applies brightness changes through whichever backend is available: PowerDevil when
    running under KDE Plasma 6 (preferred - keeps Plasma's own brightness UI in sync),
    ddcutil directly everywhere else.
    """

    def __init__(self, config: Config):
        self._config = config
        self.backend: BrightnessBackend | None = (
            PowerDevilBackend.detect(config) if config.prefer_powerdevil else None
        )
        if self.backend is not None:
            log("Brightness backend: KDE PowerDevil (org.kde.ScreenBrightness)")
        else:
            self.backend = DdcutilBackend(config)
            log("Brightness backend: ddcutil (direct DDC/CI)")

        # If PowerDevil is already going to show its own OSD for every change, a desktop
        # notification on top of it would just be a redundant second popup.
        self.shows_native_osd = isinstance(self.backend, PowerDevilBackend) and config.powerdevil_show_osd

        # True while we're on the ddcutil fallback but would rather be on PowerDevil -
        # at login PowerDevil often registers on D-Bus *after* Lunos starts, so the
        # detection above misses it. maybe_adopt_powerdevil() keeps re-checking.
        self._powerdevil_pending = config.prefer_powerdevil and not isinstance(self.backend, PowerDevilBackend)
        self._next_powerdevil_redetect_monotonic = time.monotonic() + config.powerdevil_redetect_interval_seconds

    def maybe_adopt_powerdevil(self, current_pct: int) -> bool:
        """
        Rate-limited re-detection of PowerDevil while running on the ddcutil fallback.
        When it appears, switches the backend over and immediately writes the tracked
        brightness through PowerDevil once: PowerDevil caches the brightness it read
        when *it* enumerated the display, so any ddcutil writes Lunos made before the
        switch left that cache stale - a manual brightness key press would then step
        from the stale value (e.g. 40% -> 45%) instead of the real one (5% -> 10%).
        The sync write corrects the cache and Plasma's slider. Returns True on switch.
        """
        if not self._powerdevil_pending:
            return False
        now = time.monotonic()
        if now < self._next_powerdevil_redetect_monotonic:
            return False
        self._next_powerdevil_redetect_monotonic = now + self._config.powerdevil_redetect_interval_seconds

        backend = PowerDevilBackend.detect(self._config)
        if backend is None:
            return False

        self.backend = backend
        self._powerdevil_pending = False
        self.shows_native_osd = self._config.powerdevil_show_osd
        log("PowerDevil appeared on D-Bus; switching brightness backend to it")
        try:
            backend.set_pct(current_pct)  # sync PowerDevil's cached value / Plasma's slider
        except RuntimeError as error:
            log(f"Could not sync brightness to PowerDevil after switching: {error}")
        return True

    def get_current_brightness_pct(self) -> int | None:
        return self.backend.get_current_pct()

    def ramp_to(self, from_pct: int, to_pct: int) -> None:
        """
        Steps brightness from from_pct to to_pct, mimicking a real display's smooth
        dim/brighten instead of an instant jump - only on backends that need it
        (PowerDevil already handles this itself). Step count is capped at
        max_transition_steps: a normal single-bucket change collapses to one
        instant call, while a large jump (e.g. a flashlight pointed at the sensor)
        gets a short, bounded staircase instead of one big jump - without turning
        into a multi-second slideshow of DDC/CI round-trips.
        """
        delta = to_pct - from_pct
        if delta == 0:
            return

        if not self.backend.supports_ramping:
            self.backend.set_pct(to_pct)
            return

        ideal_steps = math.ceil(abs(delta) / self._config.transition_step_granularity_pct)
        step_count = max(1, min(self._config.max_transition_steps, ideal_steps))
        for step in range(1, step_count + 1):
            intermediate = round(from_pct + delta * step / step_count)
            self.backend.set_pct(intermediate)
            if step < step_count:
                time.sleep(self._config.transition_step_delay_seconds)


# --------------------------------------------------------------------------- #
# Lux filtering (moving median)
# --------------------------------------------------------------------------- #

class LuxMedianFilter:
    """Suppresses single-sample outliers/spikes in the raw lux stream via a moving median."""

    def __init__(self, config: Config):
        self._raw_history: deque[float] = deque(maxlen=config.median_window)

    @property
    def sample_count(self) -> int:
        return len(self._raw_history)

    def add_reading(self, raw_lux: float) -> float:
        self._raw_history.append(raw_lux)
        return sorted(self._raw_history)[len(self._raw_history) // 2]


# --------------------------------------------------------------------------- #
# Update rate limiting
# --------------------------------------------------------------------------- #

class BrightnessUpdateGate:
    """Prevents ddcutil calls from firing too close together in time."""

    def __init__(self, config: Config):
        self._config = config
        self._last_update_monotonic: float = 0.0

    def enough_time_passed(self) -> bool:
        return time.monotonic() - self._last_update_monotonic >= self._config.min_seconds_between_updates

    def mark_applied(self) -> None:
        self._last_update_monotonic = time.monotonic()


# --------------------------------------------------------------------------- #
# Manual override detection
# --------------------------------------------------------------------------- #

class ManualOverrideGuard:
    """
    Detects brightness changes made outside the daemon (e.g. keyboard brightness
    keys) by periodically comparing the monitor's actual brightness against what
    the daemon last applied. On a mismatch, automatic adjustment is paused for a
    cooldown period instead of immediately overriding the manual change - mirroring
    macOS, which respects a manual brightness change for a while before resuming
    automatic control from that new baseline.

    The mismatch is also remembered as a standing offset_pct (actual minus the
    target of the bucket the *ambient light currently selects*), which future
    automatic adjustments add on top of their own target - e.g. if you nudge the
    brightness 10% brighter than what Lunos picked, later bucket changes land 10%
    brighter too, instead of snapping back to the table's bare values every time.

    The reference is the ambient-selected bucket - the same bucket the offset is
    later added back to - so the delta is measured against exactly what it will be
    applied to. Measuring against the bucket nearest the new brightness instead
    would let the reference jump a whole bucket for a small manual nudge, flipping
    the offset's sign even though the user's intent barely changed.

    It's replaced (not accumulated) by the next detected manual change. When
    config.offset_state_file is set (the default), the offset is also persisted
    there on every change and restored at startup, so a standing manual preference
    (e.g. "always 10% brighter than the table") survives restarts and reboots.
    Only the offset survives - the override *cooldown* is deliberately not
    persisted, since "pause auto-adjust for a while" is a reaction to a moment,
    not a standing preference.
    """

    def __init__(self, config: Config, monitor: MonitorController):
        self._config = config
        self._monitor = monitor
        # Seed the poll timer to "now" so the first override check is deferred by a full
        # poll interval. time.monotonic() is already large at boot (seconds since boot),
        # so a 0.0 start would let the very first lux reading poll immediately - comparing
        # two independent, not-yet-settled boot-time brightness reads and misreading the
        # difference as a manual change. Starting the clock here gives the display/DDC-CI
        # a moment to settle before the guard is allowed to react.
        self._last_poll_monotonic: float = time.monotonic()
        self._override_until_monotonic: float = 0.0
        self._state_path: Path | None = (
            Path(config.offset_state_file).expanduser() if config.offset_state_file else None
        )
        self.offset_pct: int = self._load_offset()
        if self.offset_pct:
            log(f"Restored manual brightness offset: {self.offset_pct:+d}%")

    def _load_offset(self) -> int:
        """Reads the persisted offset; any problem (missing/corrupt file, wrong type)
        just means starting from 0, exactly like before persistence existed."""
        if self._state_path is None:
            return 0
        try:
            offset = json.loads(self._state_path.read_text())["offset_pct"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            return 0
        if not isinstance(offset, int):
            return 0
        # A stale file from a different monitor/bucket table could hold a nonsensical
        # value; brightness percentages bound the sane offset range to (-100, 100).
        return max(-99, min(99, offset))

    def _save_offset(self) -> None:
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename so a crash mid-write can't leave a truncated file -
            # os.replace is atomic within the same directory/filesystem.
            tmp_path = self._state_path.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps({"offset_pct": self.offset_pct}))
            os.replace(tmp_path, self._state_path)
        except OSError as error:
            log(f"Could not persist manual brightness offset: {error}")

    def active(self) -> bool:
        return time.monotonic() < self._override_until_monotonic

    def check(self, tracked_pct: int, ambient_target_pct: int) -> int | None:
        """
        Rate-limited poll of the monitor's actual brightness. Returns the actual
        percentage (and starts/refreshes the cooldown, and recomputes offset_pct
        as the manual value's delta from ambient_target_pct - the target of the
        bucket the ambient light currently selects, which is the same bucket the
        offset is later added back to) if it no longer matches tracked_pct, or
        None if nothing changed or it isn't time to poll yet.
        """
        now = time.monotonic()
        if now - self._last_poll_monotonic < self._config.override_poll_interval_seconds:
            return None
        self._last_poll_monotonic = now

        actual_pct = self._monitor.get_current_brightness_pct()
        if actual_pct is None:
            return None

        if abs(actual_pct - tracked_pct) > self._config.manual_override_tolerance_pct:
            self._override_until_monotonic = now + self._config.manual_override_cooldown_seconds
            self.offset_pct = actual_pct - ambient_target_pct
            self._save_offset()
            return actual_pct

        return None


# --------------------------------------------------------------------------- #
# Sensor stream (SSE)
# --------------------------------------------------------------------------- #

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


class StaleSensorData(RuntimeError):
    """Raised when the SSE connection stays open but stops producing valid lux readings."""


def read_ambient_lux_values(config: Config):
    """
    Generator: connects to the sensor's SSE stream and continuously yields lux
    values from the ambient-light channel. Ignores all other sensor channels
    (e.g. raw full_spectrum/infrared values) as well as invalid/empty events.

    Besides sensor-state events, the firmware also pushes its own log lines
    (e.g. sensor saturation warnings) over the same stream; those are surfaced
    as readable log messages instead of being treated as parse errors. If no
    valid lux reading arrives for config.stale_reading_timeout_seconds - e.g.
    because the sensor is saturated by direct/bright light and has stopped
    publishing readings - StaleSensorData is raised so the caller reconnects
    instead of waiting forever.
    """
    response = requests.get(
        config.sensor_url, stream=True, timeout=config.connection_timeout_seconds
    )
    try:
        client = sseclient.SSEClient(response)

        last_valid_reading_monotonic = time.monotonic()

        for event in client.events():
            if time.monotonic() - last_valid_reading_monotonic > config.stale_reading_timeout_seconds:
                raise StaleSensorData(
                    f"No valid lux reading in over {config.stale_reading_timeout_seconds:.0f}s "
                    f"(sensor may be saturated or stuck)"
                )

            if not event.data or not event.data.strip():
                continue  # keep-alive / empty line

            try:
                payload = json.loads(event.data)
            except json.JSONDecodeError:
                clean_text = ANSI_ESCAPE_RE.sub("", event.data).strip()
                log(f"[sensor] {clean_text}")
                continue

            if payload.get("id") != config.sensor_event_id:
                continue  # different sensor channel, not relevant

            lux = payload.get("value")
            if lux is not None:
                # Reset the staleness clock on every valid reading; otherwise the timeout
                # measures time-since-connect and tears down a perfectly healthy stream.
                last_valid_reading_monotonic = time.monotonic()
                yield float(lux)
    finally:
        response.close()


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

def run(config: Config) -> None:
    monitor = MonitorController(config)
    median_filter = LuxMedianFilter(config)
    update_gate = BrightnessUpdateGate(config)
    override_guard = ManualOverrideGuard(config, monitor)

    log("Lunos starting...")

    current_pct = monitor.get_current_brightness_pct()
    if current_pct is None:
        current_pct = config.buckets[config.default_bucket_index].brightness_pct
        current_bucket_index = config.default_bucket_index
        log(f"Could not read current monitor brightness, assuming {current_pct}%.")
    else:
        # Anchor to whichever bucket's target is closest to the monitor's actual current
        # brightness, not a hardcoded default - otherwise a reading that happens to fall in
        # the assumed bucket's range never triggers an update, even if the real brightness
        # doesn't match that bucket's target at all.
        current_bucket_index = nearest_bucket_index_for_pct(config.buckets, current_pct)
        log(f"Current monitor brightness: {current_pct}% (Bucket: {current_bucket_index + 1})")

    while True:
        try:
            log(f"Connecting to sensor at {config.sensor_url} ...")
            for raw_lux in read_ambient_lux_values(config):
                if median_filter.sample_count == 0:
                    log("Connected, waiting for lux values.")

                # PowerDevil may have started after us (login race) - upgrade to it when it shows up.
                monitor.maybe_adopt_powerdevil(current_pct)

                smoothed_lux = median_filter.add_reading(raw_lux)
                target_bucket_index = select_bucket_index(config.buckets, smoothed_lux, current_bucket_index)
                target_bucket_pct = config.buckets[target_bucket_index].brightness_pct

                log(
                    f"Raw: {raw_lux:.1f} lx | Median: {smoothed_lux:.1f} lx "
                    f"| Bucket: {target_bucket_index + 1} ({target_bucket_pct}%) "
                    f"| Brightness: {current_pct}% "
                    f"| Offset: {override_guard.offset_pct:+d}%"
                )

                override_pct = override_guard.check(current_pct, target_bucket_pct)

                if override_pct is not None:
                    current_pct = override_pct
                    # Anchor to the ambient-selected bucket, not the bucket nearest the manual
                    # brightness: the offset now carries the manual delta relative to this bucket,
                    # and re-anchoring to a different bucket would double-count that delta.
                    current_bucket_index = target_bucket_index
                    log(
                        f"Manual brightness change: {current_pct}% (Offset: {override_guard.offset_pct:+d}%) \n"
                        f"Pausing auto-adjustment ({config.manual_override_cooldown_seconds:.0f}s)"
                    )
                    notify(f"Manual brightness change to {current_pct}%. \n Pausing auto-adjustment for {(config.manual_override_cooldown_seconds / 60):.0f} Minutes.", config)
                    continue

                if target_bucket_index == current_bucket_index:
                    continue
                if override_guard.active():
                    continue
                if not update_gate.enough_time_passed():
                    continue

                target_pct = max(config.min_brightness_pct, min(100, target_bucket_pct + override_guard.offset_pct))

                try:
                    monitor.ramp_to(current_pct, target_pct)
                    current_pct = target_pct
                    current_bucket_index = target_bucket_index
                    update_gate.mark_applied()
                    log(f"Brightness set: {target_pct}% (at {smoothed_lux:.1f} lx)")
                    if not monitor.shows_native_osd:
                        notify(f"Brightness: {target_pct}% ({smoothed_lux:.1f} lx)", config)
                except RuntimeError as error:
                    log(f"ERROR while setting brightness ({target_pct}%): {error}")
                    notify(f"Error setting brightness: {str(error)[:80]}", config)

        except Exception as error:
            log(f"Sensor connection lost: {error}, retry in {config.reconnect_delay_seconds}s")
            time.sleep(config.reconnect_delay_seconds)


if __name__ == "__main__":
    run(Config())
