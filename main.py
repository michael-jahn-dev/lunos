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
import re
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime


import requests
import sseclient


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

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

    median_window: int = 3          # number of raw samples in the moving-median filter (swallows single spikes)

    # Ramp tuning: each step is a real ddcutil round-trip over DDC/CI (slow, often
    # a few hundred ms on real hardware - unlike a laptop's near-instant PWM backlight),
    # so step count is capped regardless of how big the brightness delta is.
    transition_step_granularity_pct: int = 15  # ideal brightness change per step
    max_transition_steps: int = 4              # hard cap on steps per ramp, bounds worst-case latency
    transition_step_delay_seconds: float = 0.05  # extra pacing delay between steps

    min_seconds_between_updates: float = 2.0  # minimum gap between two applied brightness changes

    # Manual-override detection (mirrors macOS: a manual brightness change is respected
    # for a while instead of being immediately overridden by the next auto-adjustment).
    override_poll_interval_seconds: float = 10.0   # how often to check actual vs. tracked brightness
    manual_override_tolerance_pct: int = 3          # mismatch beyond this counts as a manual change
    manual_override_cooldown_seconds: float = 300.0  # how long to pause auto-adjustment afterwards

    # If the SSE connection stays open but no valid lux reading arrives for this long
    # (e.g. the sensor is saturated by direct light and stops publishing readings),
    # force a reconnect instead of sitting idle indefinitely.
    stale_reading_timeout_seconds: float = 90.0

    reconnect_delay_seconds: float = 5.0        # wait time before retrying a dropped/failed SSE connection
    connection_timeout_seconds: float = 30.0    # connect + read timeout for the SSE HTTP request

    notifications_enabled: bool = True   # show a desktop notification (via notify-send) on brightness changes
    notification_timeout_ms: int = 2000  # how long a desktop notification stays visible


# --------------------------------------------------------------------------- #
# Bucketed lux -> brightness curve (modeled on Windows' bucketed ALR curve)
# --------------------------------------------------------------------------- #

# Overlapping (min_lux, max_lux, brightness_pct) buckets. The overlap is intentional:
# it's what gives hysteresis "for free", the same way Windows 11's default ambient
# light response curve avoids flicker without a separately tuned threshold.
BUCKETS: tuple[tuple[float, float, int], ...] = (
    (0, 10, 5),
    (5, 50, 20),
    (15, 100, 35),
    (60, 300, 50),
    (150, 400, 65),
    (250, 650, 80),
    (350, 1000, 100),
)

DEFAULT_BUCKET_INDEX = 1  # bucket 2: the most common indoor lighting condition, same default as Windows


def nearest_bucket_index_for_pct(pct: int) -> int:
    """Finds the bucket whose target percentage is closest to a given brightness."""
    return min(range(len(BUCKETS)), key=lambda i: abs(BUCKETS[i][2] - pct))


def select_bucket_index(lux: float, current_index: int) -> int:
    """
    Picks the bucket for a lux reading: stays in the current bucket if it still
    contains the reading (this is the hysteresis), otherwise moves to the
    containing bucket closest to the current one.
    """
    containing = [i for i, (lo, hi, _) in enumerate(BUCKETS) if lo <= lux <= hi]
    if not containing:
        return 0 if lux < BUCKETS[0][0] else len(BUCKETS) - 1
    if current_index in containing:
        return current_index
    return min(containing, key=lambda i: abs(i - current_index))


# --------------------------------------------------------------------------- #
# Logging / notifications
# --------------------------------------------------------------------------- #

def log(message: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def notify(message: str, config: Config) -> None:
    if not config.notifications_enabled:
        return
    subprocess.run(
        ["notify-send", "-t", str(config.notification_timeout_ms), "Lunos", message],
        check=False,
    )


# --------------------------------------------------------------------------- #
# Monitor control backends
# --------------------------------------------------------------------------- #

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
POWERDEVIL_SUPPRESS_INDICATOR_FLAG = 1  # don't pop up Plasma's own OSD for automated changes


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

    def __init__(self, display_path: str):
        self._display_path = display_path

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

            return PowerDevilBackend(display_path)

        return None

    def get_current_pct(self) -> int | None:
        brightness = _busctl_get_property(
            POWERDEVIL_SERVICE, self._display_path, POWERDEVIL_DISPLAY_INTERFACE, "Brightness"
        )
        max_brightness = _busctl_get_property(
            POWERDEVIL_SERVICE, self._display_path, POWERDEVIL_DISPLAY_INTERFACE, "MaxBrightness"
        )
        if brightness is None or not max_brightness:
            return None
        return round(brightness / max_brightness * 100)

    def set_pct(self, pct: int) -> None:
        max_brightness = _busctl_get_property(
            POWERDEVIL_SERVICE, self._display_path, POWERDEVIL_DISPLAY_INTERFACE, "MaxBrightness"
        )
        if not max_brightness:
            raise RuntimeError("Could not read MaxBrightness from PowerDevil")

        native_value = round(pct / 100 * max_brightness)
        ok = _busctl_call(
            POWERDEVIL_SERVICE, self._display_path, POWERDEVIL_DISPLAY_INTERFACE,
            "SetBrightness", "iu", native_value, POWERDEVIL_SUPPRESS_INDICATOR_FLAG,
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
        self.backend = PowerDevilBackend.detect(config) if config.prefer_powerdevil else None
        if self.backend is not None:
            log("Brightness backend: KDE PowerDevil (org.kde.ScreenBrightness)")
        else:
            self.backend = DdcutilBackend(config)
            log("Brightness backend: ddcutil (direct DDC/CI)")

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
    """

    def __init__(self, config: Config, monitor: MonitorController):
        self._config = config
        self._monitor = monitor
        self._last_poll_monotonic: float = 0.0
        self._override_until_monotonic: float = 0.0

    def active(self) -> bool:
        return time.monotonic() < self._override_until_monotonic

    def check(self, tracked_pct: int) -> int | None:
        """
        Rate-limited poll of the monitor's actual brightness. Returns the actual
        percentage (and starts/refreshes the cooldown) if it no longer matches
        tracked_pct, or None if nothing changed or it isn't time to poll yet.
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
            yield float(lux)


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
        current_pct = BUCKETS[DEFAULT_BUCKET_INDEX][2]
        current_bucket_index = DEFAULT_BUCKET_INDEX
        log(f"Could not read current monitor brightness, assuming {current_pct}%.")
    else:
        # Anchor to whichever bucket's target is closest to the monitor's actual current
        # brightness, not a hardcoded default - otherwise a reading that happens to fall in
        # the assumed bucket's range never triggers an update, even if the real brightness
        # doesn't match that bucket's target at all.
        current_bucket_index = nearest_bucket_index_for_pct(current_pct)
        log(f"Current monitor brightness: {current_pct}% (starting in bucket {current_bucket_index + 1})")

    while True:
        try:
            log(f"Connecting to sensor at {config.sensor_url} ...")
            for raw_lux in read_ambient_lux_values(config):
                if median_filter.sample_count == 0:
                    log("Connected, waiting for lux values.")

                smoothed_lux = median_filter.add_reading(raw_lux)
                target_bucket_index = select_bucket_index(smoothed_lux, current_bucket_index)

                log(
                    f"Raw: {raw_lux:.1f} lx | Median: {smoothed_lux:.1f} lx "
                    f"| bucket {target_bucket_index + 1} -> {BUCKETS[target_bucket_index][2]}%"
                )

                override_pct = override_guard.check(current_pct)
                if override_pct is not None:
                    current_pct = override_pct
                    current_bucket_index = nearest_bucket_index_for_pct(current_pct)
                    log(
                        f"Manual brightness change detected: now {current_pct}% - pausing "
                        f"automatic adjustment for {config.manual_override_cooldown_seconds:.0f}s"
                    )
                    notify(f"Manual brightness detected ({current_pct}%), auto-adjust paused", config)
                    continue

                if target_bucket_index == current_bucket_index:
                    continue
                if override_guard.active():
                    continue
                if not update_gate.enough_time_passed():
                    continue

                target_pct = BUCKETS[target_bucket_index][2]
                try:
                    monitor.ramp_to(current_pct, target_pct)
                    current_pct = target_pct
                    current_bucket_index = target_bucket_index
                    update_gate.mark_applied()
                    log(f"Brightness set: {target_pct}% (at {smoothed_lux:.1f} lx)")
                    notify(f"Brightness: {target_pct}% ({smoothed_lux:.1f} lx)", config)
                except RuntimeError as error:
                    log(f"ERROR while setting brightness ({target_pct}%): {error}")
                    notify(f"Error setting brightness: {str(error)[:80]}", config)

        except Exception as error:
            log(f"Sensor connection lost: {error}, retry in {config.reconnect_delay_seconds}s")
            time.sleep(config.reconnect_delay_seconds)


if __name__ == "__main__":
    run(Config())
