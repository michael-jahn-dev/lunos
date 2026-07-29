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
import queue
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, fields as dataclass_fields, replace
from pathlib import Path
from typing import Any, Callable, NamedTuple, Protocol
from urllib.parse import urlparse


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
# Config file overlay
#
# The dataclass above stays the schema, the defaults and the documentation; the
# file below holds *only explicit overrides*. An absent or unreadable file
# therefore means exactly today's behavior, which is what keeps an existing
# install working unchanged after an upgrade.
#
# The daemon is the only writer (the tray app sends set_config over the control
# socket instead of writing the file itself), so there is one writer, no locking
# and no merge logic.
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG_FILE = "~/.config/lunos/config.json"
CONFIG_FILE_ENV_VAR = "LUNOS_CONFIG_FILE"  # mostly for tests and for running two instances side by side


class FieldSpec(NamedTuple):
    """
    Everything that is known about a Config field beyond its default: how to
    validate it, what changing it costs at runtime, and how a GUI should render
    it. One hand-maintained table feeds all three (validation, the apply matrix,
    and the `get_schema` reply the tray builds its settings window from), so a
    new setting is a single entry here rather than three places to forget.
    """

    kind: str                     # "bool" | "int" | "float" | "str" | "url" | "path" | "buckets"
    section: str                  # which settings tab a GUI should put it on
    label: str
    apply: str = "hot"            # "hot" (next loop iteration) | "reconnect" (drops the SSE stream) | "restart"
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    unit: str | None = None
    optional: bool = False        # None is a meaningful value (e.g. "no display filter")
    help: str = ""


FIELD_SPECS: dict[str, FieldSpec] = {
    "sensor_url": FieldSpec(
        "url", "sensor", "Sensor URL", apply="reconnect",
        help="The sensor's server-sent-events endpoint. Use its .local name unless mDNS is "
             "unreliable on your network, in which case use the IP address. Changing this "
             "re-opens the stream immediately.",
    ),
    "sensor_event_id": FieldSpec(
        "str", "sensor", "Sensor event id", apply="reconnect",
        help="Which channel of the stream carries lux values. The sensor also publishes other "
             "channels (raw infrared, full spectrum) that must be ignored. This id comes from your "
             "device's firmware and can differ from lunar.fyi's documentation example - if Lunos "
             "connects but never reads a value, this is almost always why.",
    ),
    "connection_timeout_seconds": FieldSpec(
        "float", "sensor", "Connection timeout", apply="reconnect",
        minimum=1.0, maximum=600.0, step=1.0, unit="s",
        help="How long to wait for the sensor to answer before giving up and retrying. Raise it if "
             "a slow or busy Wi-Fi link causes needless reconnects.",
    ),
    "stale_reading_timeout_seconds": FieldSpec(
        "float", "sensor", "Stale-reading timeout", apply="reconnect",
        minimum=5.0, maximum=3600.0, step=5.0, unit="s",
        help="A sensor saturated by direct light can stop publishing readings while its connection "
             "stays open. After this long without a valid value, Lunos reconnects instead of waiting "
             "forever. Too low and a genuinely dark, quiet room triggers pointless reconnects.",
    ),
    "reconnect_delay_seconds": FieldSpec(
        "float", "sensor", "Reconnect delay",
        minimum=0.5, maximum=300.0, step=0.5, unit="s",
        help="Pause before retrying after the connection drops - which is also how long a sensor "
             "that is switched off keeps this many log lines coming. Commands from this app can be "
             "delayed by up to this long while the sensor is down.",
    ),
    "buckets": FieldSpec(
        "buckets", "curve", "Lux to brightness curve",
        help="Each row maps a lux range to one brightness level. Neighbouring rows should overlap: "
             "while a reading stays inside the current row's range the brightness does not change, "
             "and that is the entire flicker protection. Rows must climb in brightness from top to "
             "bottom. Tune the lux ranges to your room and the percentages to your monitor.",
    ),
    "default_bucket_index": FieldSpec(
        "int", "curve", "Fallback bucket", apply="restart",
        minimum=0, maximum=99, step=1,
        help="Which row to assume at startup when the monitor's current brightness cannot be read "
             "at all (no DDC/CI answer). Normally unused: Lunos anchors to the brightness the "
             "monitor actually reports. Counted from 0.",
    ),
    "min_brightness_pct": FieldSpec(
        "int", "behaviour", "Minimum brightness",
        minimum=0, maximum=100, step=1, unit="%",
        help="Brightness never goes below this, even after the offset below is subtracted. It exists "
             "so a large negative offset on a dark row cannot drive the panel to 0% and leave you "
             "with a black screen.",
    ),
    "min_seconds_between_updates": FieldSpec(
        "float", "behaviour", "Minimum gap between updates",
        minimum=0.0, maximum=600.0, step=0.5, unit="s",
        help="Rate limit between two applied brightness changes. Light that keeps crossing a row "
             "boundary (a cloudy day, a flickering lamp) otherwise turns into a stream of writes to "
             "the monitor.",
    ),
    "median_window": FieldSpec(
        "int", "behaviour", "Median filter window",
        minimum=1, maximum=99, step=1, unit="samples",
        help="How many recent readings the median is taken over, which is what stops a single spike "
             "(a phone torch, a passing headlight) from moving the brightness. Higher is steadier "
             "but slower to react; 1 disables smoothing entirely.",
    ),
    "transition_step_granularity_pct": FieldSpec(
        "int", "behaviour", "Ramp step size",
        minimum=1, maximum=100, step=1, unit="%",
        help="Target size of one step when easing into a big brightness change instead of jumping. "
             "Smaller looks smoother but costs one slow monitor write per step. Unused on the "
             "PowerDevil backend, which smooths changes itself.",
    ),
    "max_transition_steps": FieldSpec(
        "int", "behaviour", "Maximum ramp steps",
        minimum=1, maximum=50, step=1,
        help="Hard cap on steps per change, so even a jump from 5% to 100% stays quick rather than "
             "becoming a slideshow. A normal one-row change is a single step anyway. Unused on the "
             "PowerDevil backend.",
    ),
    "transition_step_delay_seconds": FieldSpec(
        "float", "behaviour", "Delay between ramp steps",
        minimum=0.0, maximum=5.0, step=0.05, unit="s",
        help="Extra pause between individual ramp steps. Monitors that garble rapid DDC/CI writes "
             "settle down with a little more here. Unused on the PowerDevil backend.",
    ),
    "override_poll_interval_seconds": FieldSpec(
        "float", "behaviour", "Manual-change poll interval",
        minimum=0.5, maximum=600.0, step=0.5, unit="s",
        help="How often Lunos reads the monitor's real brightness to notice changes it did not make "
             "itself - the monitor's own buttons, or the keyboard's brightness keys. Each poll is a "
             "real read from the display, so very short intervals mean constant traffic.",
    ),
    "manual_override_tolerance_pct": FieldSpec(
        "int", "behaviour", "Manual-change tolerance",
        minimum=0, maximum=100, step=1, unit="%",
        help="How far the monitor may differ from the last applied value before it counts as your "
             "doing. A few percent of slack is needed because monitors round the value they report; "
             "set it to 0 and that rounding alone looks like a manual change.",
    ),
    "manual_override_cooldown_seconds": FieldSpec(
        "float", "behaviour", "Manual-change cooldown",
        minimum=0.0, maximum=86400.0, step=30.0, unit="s",
        help="After a manual change is detected, automatic adjustment stays out of the way for this "
             "long, then resumes from your new value. The difference is also remembered as the "
             "offset above. Setting the offset here in Lunos ends the cooldown at once - asking "
             "Lunos for a brightness is asking it to act, not to back off.",
    ),
    "offset_state_file": FieldSpec(
        "path", "behaviour", "Offset state file", optional=True,
        help="File the standing offset is written to so it survives restarts and reboots. The pause "
             "after a manual change is deliberately not saved - that is a reaction to a moment, not "
             "a lasting preference. Leave empty to have the offset reset to 0 on every start.",
    ),
    "prefer_powerdevil": FieldSpec(
        "bool", "backend", "Prefer KDE PowerDevil",
        help="On KDE Plasma, let PowerDevil set the brightness instead of Lunos driving the monitor "
             "directly. Recommended there: Plasma's own slider and OSD stay in step, and two "
             "programs never fight over the same monitor. Falls back to ddcutil automatically "
             "wherever PowerDevil is absent, so leaving this on costs nothing on other desktops.",
    ),
    "powerdevil_display_label_contains": FieldSpec(
        "str", "backend", "PowerDevil display filter", optional=True,
        help="Part of a display's name, used to choose between several external monitors under "
             "PowerDevil - for example \"U2720\". Matching ignores case. Leave empty to use the "
             "first display that is not the built-in laptop panel.",
    ),
    "powerdevil_show_osd": FieldSpec(
        "bool", "backend", "Show Plasma's brightness OSD",
        help="Show Plasma's usual brightness popup for changes Lunos makes. It is also what makes "
             "the brightness applet's slider refresh, so turning it off can leave that slider "
             "showing a stale value. While it is on, Lunos skips its own notification rather than "
             "announcing the same change twice.",
    ),
    "powerdevil_redetect_interval_seconds": FieldSpec(
        "float", "backend", "PowerDevil re-detection interval",
        minimum=1.0, maximum=3600.0, step=5.0, unit="s",
        help="At login Lunos often starts before PowerDevil is ready and falls back to ddcutil. This "
             "is how often it re-checks and switches over once PowerDevil appears. Only matters "
             "while the fallback is in use; the checking stops for good after the switch.",
    ),
    "monitor_display": FieldSpec(
        "str", "backend", "ddcutil display", optional=True,
        help="Which monitor ddcutil should address when several are connected - run 'ddcutil detect' "
             "to see the numbers. Leave empty for a single monitor. Ignored while the PowerDevil "
             "backend is in use, which picks the display by name instead.",
    ),
    "notifications_enabled": FieldSpec(
        "bool", "notifications", "Desktop notifications",
        help="Pop up a desktop notification when the brightness changes, when a manual change is "
             "detected, and when setting the brightness fails. Needs notify-send. Notifications are "
             "skipped anyway while Plasma shows its own brightness popup.",
    ),
    "notification_timeout_ms": FieldSpec(
        "int", "notifications", "Notification timeout",
        minimum=0, maximum=600000, step=500, unit="ms",
        help="How long a notification stays on screen. Some notification daemons enforce their own "
             "limits and ignore this.",
    ),
}


class ConfigFieldError(ValueError):
    """A single rejected field value, carrying a message meant for a UI."""


def config_file_path(path: str | os.PathLike[str] | None = None) -> Path:
    """The config file to read/write: explicit argument, env override, or the XDG default."""
    if path is not None:
        return Path(path).expanduser()
    return Path(os.environ.get(CONFIG_FILE_ENV_VAR) or DEFAULT_CONFIG_FILE).expanduser()


def _coerce_number(name: str, value: Any, spec: FieldSpec) -> int | float:
    if isinstance(value, bool):  # bool is an int subclass; "true" is not a poll interval
        raise ConfigFieldError(f"{name}: expected a number, got a boolean")
    if not isinstance(value, (int, float)):
        raise ConfigFieldError(f"{name}: expected a number, got {type(value).__name__}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigFieldError(f"{name}: must be a finite number")
    number = int(value) if spec.kind == "int" else float(value)
    if spec.kind == "int" and value != number:
        raise ConfigFieldError(f"{name}: expected a whole number, got {value}")
    if spec.minimum is not None and number < spec.minimum:
        raise ConfigFieldError(f"{name}: must be at least {spec.minimum:g}")
    if spec.maximum is not None and number > spec.maximum:
        raise ConfigFieldError(f"{name}: must be at most {spec.maximum:g}")
    return number


def _coerce_buckets(value: Any) -> tuple[Bucket, ...]:
    """Rebuilds the bucket table from a list of [min_lux, max_lux, brightness_pct] triples."""
    if not isinstance(value, (list, tuple)) or not value:
        raise ConfigFieldError("buckets: expected a non-empty list of [min_lux, max_lux, brightness_pct] triples")

    buckets: list[Bucket] = []
    for position, entry in enumerate(value, start=1):
        if not isinstance(entry, (list, tuple)) or len(entry) != 3:
            raise ConfigFieldError(f"buckets: entry {position} must be [min_lux, max_lux, brightness_pct]")
        min_lux, max_lux, brightness_pct = entry
        for number in (min_lux, max_lux, brightness_pct):
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                raise ConfigFieldError(f"buckets: entry {position} contains a non-numeric value")
        if min_lux < 0 or max_lux < 0:
            raise ConfigFieldError(f"buckets: entry {position} has a negative lux bound")
        if min_lux >= max_lux:
            raise ConfigFieldError(f"buckets: entry {position} needs min_lux < max_lux")
        if not 1 <= brightness_pct <= 100:
            raise ConfigFieldError(f"buckets: entry {position} needs a brightness between 1 and 100")
        buckets.append(Bucket(float(min_lux), float(max_lux), int(brightness_pct)))

    # select_bucket_index() walks the table by index and treats a lower index as "darker",
    # so a table that isn't ascending in brightness would make the hysteresis pick nonsense.
    for previous, current in zip(buckets, buckets[1:]):
        if current.brightness_pct <= previous.brightness_pct:
            raise ConfigFieldError("buckets: brightness percentages must increase from one bucket to the next")
    return tuple(buckets)


def coerce_config_value(name: str, value: Any) -> Any:
    """
    Validates one incoming setting against FIELD_SPECS and returns the value to
    store on Config. Raises ConfigFieldError with a message meant for a UI.

    This is the single validation path for both the config file and the control
    socket: it never setattr's an unknown name, never evaluates a value, and
    range-checks everything before it can reach the loop or the monitor.
    """
    spec = FIELD_SPECS.get(name)
    if spec is None:
        raise ConfigFieldError(f"{name}: unknown setting")

    if value is None:
        if not spec.optional:
            raise ConfigFieldError(f"{name}: cannot be empty")
        return None

    if spec.kind == "bool":
        if not isinstance(value, bool):
            raise ConfigFieldError(f"{name}: expected true or false")
        return value

    if spec.kind in ("int", "float"):
        return _coerce_number(name, value, spec)

    if spec.kind == "buckets":
        return _coerce_buckets(value)

    if not isinstance(value, str):
        raise ConfigFieldError(f"{name}: expected a string")
    text = value.strip()
    if not text:
        # An emptied text box means "unset" for an optional field, and is a mistake otherwise.
        if spec.optional:
            return None
        raise ConfigFieldError(f"{name}: cannot be empty")

    if spec.kind == "url":
        parsed = urlparse(text)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ConfigFieldError(f"{name}: must be an http:// or https:// URL")
    return text


def validate_config_overrides(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Validates a whole batch of settings, returning (accepted values, per-field errors)."""
    accepted: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name, value in raw.items():
        try:
            accepted[name] = coerce_config_value(name, value)
        except ConfigFieldError as error:
            errors[name] = str(error)
    return accepted, errors


def _serialize_config_value(name: str, value: Any) -> Any:
    if name == "buckets":
        return [[bucket.min_lux, bucket.max_lux, bucket.brightness_pct] for bucket in value]
    return value


def config_overrides(config: Config) -> dict[str, Any]:
    """The JSON-serializable diff between a config and the dataclass defaults."""
    defaults = Config()
    return {
        field.name: _serialize_config_value(field.name, getattr(config, field.name))
        for field in dataclass_fields(Config)
        if getattr(config, field.name) != getattr(defaults, field.name)
    }


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """
    Builds a Config from the defaults plus whatever the config file overrides.

    Never raises: a missing, unreadable, corrupt or half-outdated file falls back
    to the defaults for whatever it could not use, because failing to start is a
    far worse outcome than ignoring a bad setting. Unknown keys are dropped with
    a log line rather than migrated - a downgrade then doesn't silently wipe them
    from a file it can still read.
    """
    file_path = config_file_path(path)
    try:
        raw = json.loads(file_path.read_text())
    except FileNotFoundError:
        return Config()
    except (OSError, json.JSONDecodeError) as error:
        log(f"Ignoring config file {file_path}: {error}")
        return Config()

    if not isinstance(raw, dict):
        log(f"Ignoring config file {file_path}: expected a JSON object")
        return Config()

    known = {name: value for name, value in raw.items() if name in FIELD_SPECS}
    for name in raw.keys() - known.keys():
        log(f"Ignoring unknown setting in {file_path}: {name}")

    accepted, errors = validate_config_overrides(known)
    for message in errors.values():
        log(f"Ignoring invalid setting in {file_path}: {message}")
    if accepted:
        log(f"Loaded {len(accepted)} setting(s) from {file_path}")
    return replace(Config(), **accepted)


def save_config(config: Config, path: str | os.PathLike[str] | None = None) -> None:
    """
    Persists the overrides (not the defaults) with the same write-then-rename
    dance _save_offset() uses, so a crash mid-write cannot leave a truncated
    file that the next start would have to ignore.
    """
    file_path = config_file_path(path)
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = file_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(config_overrides(config), indent=2, sort_keys=True) + "\n")
        os.replace(tmp_path, file_path)
    except OSError as error:
        log(f"Could not save config to {file_path}: {error}")


def config_schema(config: Config) -> list[dict[str, Any]]:
    """
    The per-field description a GUI builds itself from: spec, default and current
    value. Handing this over the wire is what lets the tray app import nothing
    from the daemon and still stay in sync with new settings.
    """
    defaults = Config()
    schema = []
    for name, spec in FIELD_SPECS.items():
        schema.append({
            "name": name,
            "kind": spec.kind,
            "section": spec.section,
            "label": spec.label,
            "apply": spec.apply,
            "min": spec.minimum,
            "max": spec.maximum,
            "step": spec.step,
            "unit": spec.unit,
            "optional": spec.optional,
            "help": spec.help,
            "default": _serialize_config_value(name, getattr(defaults, name)),
            "current": _serialize_config_value(name, getattr(config, name)),
        })
    return schema


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
    def set_config(self, config: Config) -> None: ...


class DdcutilBackend:
    """Drives brightness directly via ddcutil (DDC/CI). Works everywhere ddcutil does."""

    supports_ramping = True  # raw ddcutil doesn't debounce/animate on its own, so Lunos ramps it

    VCP_BRIGHTNESS_CODE = "10"

    def __init__(self, config: Config):
        self._config = config

    def set_config(self, config: Config) -> None:
        # monitor_display is read here, not on MonitorController - a config swap that
        # stopped at the controller would make that setting silently do nothing.
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

    def set_config(self, config: Config) -> None:
        # powerdevil_show_osd is read here (in set_pct), so the swap has to reach the backend.
        # The cached MaxBrightness stays valid: a changed display filter re-selects the whole
        # backend instead of coming through here.
        self._config = config

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
        self.backend: BrightnessBackend | None = None
        self._select_backend()

        # Whether Lunos has itself applied a brightness change yet. Until it has, the value it
        # "tracks" is only the monitor's power-on reading it happened to observe at startup -
        # not something it authored, and on Plasma not authoritative (PowerDevil overwrites it
        # with its own remembered brightness at login). This decides, on PowerDevil adoption,
        # whether to push our value onto PowerDevil (we authored it) or adopt PowerDevil's.
        self._applied_write = False

    def _select_backend(self) -> None:
        """(Re-)picks the backend for the current config and derives the flags that key off it."""
        config = self._config
        backend = PowerDevilBackend.detect(config) if config.prefer_powerdevil else None
        if backend is not None:
            self.backend = backend
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

    @property
    def backend_name(self) -> str:
        return "powerdevil" if isinstance(self.backend, PowerDevilBackend) else "ddcutil"

    @property
    def powerdevil_pending(self) -> bool:
        return self._powerdevil_pending

    def set_config(self, config: Config) -> int | None:
        """
        Swaps in a new config at runtime. Returns the brightness the caller should
        re-anchor to when the backend was re-selected (the new backend's own reported
        value), or None when nothing needs re-anchoring.

        Changing prefer_powerdevil or the display filter re-runs backend selection, which
        also resets _applied_write: nothing has been written through the new backend yet,
        so its reported brightness is the truth - the same reasoning maybe_adopt_powerdevil
        uses when it adopts PowerDevil's value instead of forcing ours onto it.
        """
        previous = self._config
        self._config = config
        reselect = (
            config.prefer_powerdevil != previous.prefer_powerdevil
            or config.powerdevil_display_label_contains != previous.powerdevil_display_label_contains
        )
        if not reselect:
            self.backend.set_config(config)
            self.shows_native_osd = isinstance(self.backend, PowerDevilBackend) and config.powerdevil_show_osd
            # A longer/shorter re-detect interval should take effect now, not after the old one elapses.
            self._next_powerdevil_redetect_monotonic = min(
                self._next_powerdevil_redetect_monotonic,
                time.monotonic() + config.powerdevil_redetect_interval_seconds,
            )
            return None

        self._select_backend()
        self._applied_write = False
        return self.backend.get_current_pct()

    def maybe_adopt_powerdevil(self, current_pct: int, force: bool = False) -> int | None:
        """
        Re-detects PowerDevil while running on the ddcutil fallback and switches over when it
        appears. Returns the brightness percentage the caller should now treat as current (so
        it can re-anchor), or None if no switch happened.

        Two adoption cases, decided by whether Lunos has authored the current brightness:

        - We already applied a ddcutil write (self._applied_write): our tracked value is the
          real one. PowerDevil may hold a stale value cached from when it enumerated the
          display, so push ours onto it - otherwise Plasma's slider and a later manual +5%
          would step from the stale value (e.g. 40% -> 45%) instead of reality (5% -> 10%).

        - We have not written anything yet: our tracked value is only the monitor's power-on
          reading, which PowerDevil (the brightness authority on Plasma) may already have
          overwritten with its own remembered brightness at login. Adopt PowerDevil's actual
          value as the truth instead of forcing our stale observation onto it.

        Normally rate-limited to config.powerdevil_redetect_interval_seconds; pass force=True
        to bypass that. The main loop forces a re-check the moment it sees the monitor diverge
        from our tracked value, since PowerDevil's DDC write and its D-Bus registration are the
        same arrival event - so a divergence on the fallback is often PowerDevil taking over,
        not the user, and PowerDevil is detectable at that instant.
        """
        if not self._powerdevil_pending:
            return None
        now = time.monotonic()
        if not force and now < self._next_powerdevil_redetect_monotonic:
            return None
        self._next_powerdevil_redetect_monotonic = now + self._config.powerdevil_redetect_interval_seconds

        backend = PowerDevilBackend.detect(self._config)
        if backend is None:
            return None

        self.backend = backend
        self._powerdevil_pending = False
        self.shows_native_osd = self._config.powerdevil_show_osd
        log("PowerDevil appeared on D-Bus; switching brightness backend to it")

        if self._applied_write:
            try:
                backend.set_pct(current_pct)  # correct PowerDevil's stale cache / Plasma's slider
            except RuntimeError as error:
                log(f"Could not sync brightness to PowerDevil after switching: {error}")
            return current_pct

        adopted = backend.get_current_pct()
        if adopted is None:
            return current_pct
        if adopted != current_pct:
            log(f"Re-anchoring tracked brightness to PowerDevil: {current_pct}% -> {adopted}%")
        return adopted

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

        # From here a write is guaranteed: our tracked brightness is now self-authored, so a
        # later PowerDevil adoption should push this value rather than adopt PowerDevil's.
        self._applied_write = True

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

    def set_config(self, config: Config) -> None:
        """Resizes the window in place. deque(iterable, maxlen=n) keeps the *newest* n
        samples, so shrinking the window doesn't throw away the recent history and
        make the filter behave as if the daemon had just started."""
        if config.median_window == self._raw_history.maxlen:
            return
        self._raw_history = deque(self._raw_history, maxlen=config.median_window)

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

    def set_config(self, config: Config) -> None:
        self._config = config

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

    def set_config(self, config: Config) -> None:
        """Swaps the config, and follows offset_state_file if it moved - the in-memory
        offset is the live value, so it is written to the new location rather than
        re-read from (or left behind at) the old one."""
        previous_path = self._state_path
        self._config = config
        self._state_path = (
            Path(config.offset_state_file).expanduser() if config.offset_state_file else None
        )
        if self._state_path is not None and self._state_path != previous_path:
            self._save_offset()

    def set_offset(self, offset_pct: int) -> None:
        """
        Sets the standing offset directly, as an explicit instruction from the user
        (the tray's slider), and ends any running override cooldown.

        The cooldown exists to stop auto-adjust from fighting a change the user just
        made by hand; someone moving Lunos's own slider is asking Lunos to act, not to
        back off. Deliberately *not* routed through record_override(): that recomputes
        the offset from actual-minus-target, i.e. round-trips the number through the
        monitor and hands back a different one than the user picked.
        """
        self.offset_pct = max(-99, min(99, int(offset_pct)))
        self._override_until_monotonic = 0.0
        self._save_offset()

    def clear_override(self) -> None:
        """Ends the cooldown early, without touching the standing offset."""
        self._override_until_monotonic = 0.0

    def start_override(self, seconds: float) -> None:
        """Pauses auto-adjustment for a while without recording an offset (the tray's
        explicit 'Pause auto-adjustment', as opposed to a detected manual change)."""
        self._override_until_monotonic = time.monotonic() + seconds

    def seconds_left(self) -> float:
        return max(0.0, self._override_until_monotonic - time.monotonic())

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

    def poll_actual(self, tracked_pct: int) -> int | None:
        """
        Rate-limited read of the monitor's actual brightness. Returns it when it has
        diverged from tracked_pct beyond tolerance (a change made outside our last
        write), or None if nothing changed or it isn't time to poll yet.

        Pure detection - records no offset or cooldown. The caller decides whether the
        change is really the user's (then calls record_override) or another controller
        taking over (e.g. PowerDevil at login), which must not be recorded as a manual
        override.
        """
        now = time.monotonic()
        if now - self._last_poll_monotonic < self._config.override_poll_interval_seconds:
            return None
        self._last_poll_monotonic = now

        actual_pct = self._monitor.get_current_brightness_pct()
        if actual_pct is None:
            return None
        if abs(actual_pct - tracked_pct) <= self._config.manual_override_tolerance_pct:
            return None
        return actual_pct

    def record_override(self, actual_pct: int, ambient_target_pct: int) -> None:
        """
        Registers a confirmed manual brightness change: pause auto-adjustment for the
        cooldown and remember the standing offset (actual minus ambient_target_pct - the
        target of the bucket the ambient light currently selects, which is the same bucket
        the offset is later added back to).
        """
        self._override_until_monotonic = time.monotonic() + self._config.manual_override_cooldown_seconds
        self.offset_pct = actual_pct - ambient_target_pct
        self._save_offset()

    def check(self, tracked_pct: int, ambient_target_pct: int) -> int | None:
        """
        Convenience: poll and, on a detected change, record it as a manual override.
        The main loop instead calls poll_actual/record_override separately so it can
        re-check for a PowerDevil handoff in between.
        """
        actual_pct = self.poll_actual(tracked_pct)
        if actual_pct is not None:
            self.record_override(actual_pct, ambient_target_pct)
        return actual_pct


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

PROTOCOL_VERSION = 1  # bumped when the command set or the state snapshot changes shape

COMMAND_DRAIN_SLICE_SECONDS = 0.25  # how finely the reconnect wait is chopped up to stay responsive


class Daemon:
    """
    The reconnect loop, its state, and the only place that state is mutated.

    Everything the loop owns (tracked brightness, current bucket, the filter, the
    guard, the live config) belongs to the loop thread. The control server runs in
    a separate thread and never touches any of it directly: commands go through
    submit(), which the loop drains at the top of each iteration, and the loop
    publishes a plain-dict snapshot under a lock for readers to copy.

    The cost is latency - a command takes effect on the next lux reading (~1s), or
    after the current connection attempt when the sensor is down. That is the price
    of not putting a lock around the loop's own state, and it is invisible in a
    settings UI.
    """

    protocol_version = PROTOCOL_VERSION  # read by control.py, which imports nothing from here

    def __init__(self, config: Config, config_path: str | os.PathLike[str] | None = None):
        self.config = config
        self._config_path = config_path
        self.monitor = MonitorController(config)
        self.median_filter = LuxMedianFilter(config)
        self.update_gate = BrightnessUpdateGate(config)
        self.override_guard = ManualOverrideGuard(config, self.monitor)

        self.current_pct: int = 0
        self.current_bucket_index: int = 0

        self._commands: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._snapshot_lock = threading.Lock()
        self._snapshot: dict[str, Any] = {}
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

        self._paused = False               # explicit "pause auto-adjustment", no expiry
        self._reconnect_requested = False  # a sensor setting changed; drop the stream and re-open it
        self._stop_requested = False       # clean exit, so systemd's Restart=always restarts us
        # A change (offset, curve, ...) that must be applied even though the bucket didn't move.
        # Without this, dragging the offset slider in a room with steady light does nothing until
        # the light changes, because the loop only acts on bucket transitions.
        self._target_dirty = False

        self._sensor_connected = False
        self._last_error: str | None = None
        self._raw_lux: float | None = None
        self._median_lux: float | None = None

    # ----------------------------------------------------------------- #
    # Thread-safe surface (called from the control server's threads)
    # ----------------------------------------------------------------- #

    def add_snapshot_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        """Registers a callback invoked on the loop thread after every published
        snapshot. Listeners must not block - the control server only hands the dict
        to per-connection queues."""
        self._listeners.append(listener)

    def snapshot(self) -> dict[str, Any]:
        with self._snapshot_lock:
            return dict(self._snapshot)

    def submit(self, name: str, payload: dict[str, Any] | None = None) -> None:
        self._commands.put((name, payload or {}))

    def dispatch(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Handles one control command from any thread. Read-only commands are answered
        inline; mutating ones are validated here (validation is pure and synchronous)
        and then queued for the loop thread.

        Replying at validation time rather than after the effect is deliberate: a
        settings dialog would otherwise block for a full lux reading on every
        keystroke. The push stream reports what actually happened.
        """
        if name == "get_state":
            return {"ok": True, "state": self.snapshot()}

        if name == "get_schema":
            return {"ok": True, "schema": config_schema(self.config)}

        if name == "set_config":
            requested = payload.get("fields")
            if not isinstance(requested, dict) or not requested:
                return {"ok": False, "error": "set_config needs a non-empty \"fields\" object"}
            accepted, errors = validate_config_overrides(requested)
            if errors:
                return {"ok": False, "error": "invalid settings", "errors": errors}
            self.submit("set_config", {"fields": accepted})
            return {
                "ok": True,
                "applied": sorted(n for n in accepted if FIELD_SPECS[n].apply == "hot"),
                "reconnecting": sorted(n for n in accepted if FIELD_SPECS[n].apply == "reconnect"),
                "restart_required": sorted(n for n in accepted if FIELD_SPECS[n].apply == "restart"),
            }

        if name == "set_offset":
            offset = payload.get("offset_pct")
            if isinstance(offset, bool) or not isinstance(offset, int):
                return {"ok": False, "error": "set_offset needs an integer \"offset_pct\""}
            if not -99 <= offset <= 99:
                return {"ok": False, "error": "offset_pct must be between -99 and 99"}
            self.submit("set_offset", {"offset_pct": offset})
            return {"ok": True, "state": self.snapshot()}

        if name in ("pause", "resume", "reload_config", "restart"):
            seconds = payload.get("seconds")
            if name == "pause" and seconds is not None:
                if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
                    return {"ok": False, "error": "pause \"seconds\" must be a positive number"}
            self.submit(name, payload)
            return {"ok": True, "state": self.snapshot()}

        return {"ok": False, "error": f"unknown command: {name}"}

    # ----------------------------------------------------------------- #
    # Command execution (loop thread only)
    # ----------------------------------------------------------------- #

    def _drain_commands(self) -> None:
        while True:
            try:
                name, payload = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                self._execute(name, payload)
            except Exception as error:  # a bad command must never take the daemon down
                log(f"Control command {name} failed: {error}")

    def _execute(self, name: str, payload: dict[str, Any]) -> None:
        if name == "set_config":
            self.apply_config(replace(self.config, **payload["fields"]))
            save_config(self.config, self._config_path)
        elif name == "set_offset":
            self.override_guard.set_offset(payload["offset_pct"])
            self._paused = False
            self._target_dirty = True
            log(f"Manual brightness offset set to {self.override_guard.offset_pct:+d}%")
        elif name == "pause":
            seconds = payload.get("seconds")
            if seconds:
                self.override_guard.start_override(float(seconds))
                log(f"Auto-adjustment paused for {float(seconds):.0f}s")
            else:
                self._paused = True
                log("Auto-adjustment paused")
        elif name == "resume":
            self._paused = False
            self.override_guard.clear_override()
            self._target_dirty = True
            log("Auto-adjustment resumed")
        elif name == "reload_config":
            self.apply_config(load_config(self._config_path))
            log("Config file re-read")
        elif name == "restart":
            log("Restart requested; exiting so the service manager restarts us")
            self._stop_requested = True

    def apply_config(self, new_config: Config) -> None:
        """
        Swaps in a new config and re-applies it across the components, per the apply
        matrix: most fields are hot, a few need a rebuild, the sensor fields need the
        SSE stream re-opened (the running generator captured the *old* config, so
        swapping ours has no effect on it), and default_bucket_index has no runtime
        effect at all.
        """
        previous = self.config
        self.config = new_config

        adopted_pct = self.monitor.set_config(new_config)
        self.median_filter.set_config(new_config)
        self.update_gate.set_config(new_config)
        self.override_guard.set_config(new_config)

        if adopted_pct is not None:
            # The backend was re-selected; trust the new backend's own reading.
            if adopted_pct != self.current_pct:
                log(f"Re-anchoring tracked brightness after backend change: {self.current_pct}% -> {adopted_pct}%")
            self.current_pct = adopted_pct

        if adopted_pct is not None or new_config.buckets != previous.buckets:
            # The old index may not even exist in a new bucket table, and certainly no
            # longer means the same brightness.
            self.current_bucket_index = nearest_bucket_index_for_pct(new_config.buckets, self.current_pct)

        if any(
            getattr(new_config, name) != getattr(previous, name)
            for name, spec in FIELD_SPECS.items()
            if spec.apply == "reconnect"
        ):
            self._reconnect_requested = True

        self._target_dirty = True

    # ----------------------------------------------------------------- #
    # State snapshot
    # ----------------------------------------------------------------- #

    def _publish_snapshot(self) -> None:
        state = {
            "protocol": PROTOCOL_VERSION,
            "raw_lux": self._raw_lux,
            "median_lux": self._median_lux,
            "bucket_index": self.current_bucket_index,
            "bucket_count": len(self.config.buckets),
            "bucket_pct": self.config.buckets[self.current_bucket_index].brightness_pct,
            "brightness_pct": self.current_pct,
            "offset_pct": self.override_guard.offset_pct,
            "paused": self._paused,
            "override_active": self.override_guard.active(),
            "override_seconds_left": round(self.override_guard.seconds_left(), 1),
            "backend": self.monitor.backend_name,
            "powerdevil_pending": self.monitor.powerdevil_pending,
            "sensor_connected": self._sensor_connected,
            "last_error": self._last_error,
        }
        with self._snapshot_lock:
            self._snapshot = state
        for listener in list(self._listeners):
            try:
                listener(state)
            except Exception as error:
                log(f"Snapshot listener failed: {error}")

    # ----------------------------------------------------------------- #
    # Main loop
    # ----------------------------------------------------------------- #

    def anchor_to_monitor(self) -> None:
        current_pct = self.monitor.get_current_brightness_pct()
        if current_pct is None:
            # default_bucket_index is only ever read here, and only when the monitor cannot
            # be read at all. Clamp it: a shrunken bucket table would otherwise index out.
            index = min(self.config.default_bucket_index, len(self.config.buckets) - 1)
            self.current_pct = self.config.buckets[index].brightness_pct
            self.current_bucket_index = index
            log(f"Could not read current monitor brightness, assuming {self.current_pct}%.")
        else:
            # Anchor to whichever bucket's target is closest to the monitor's actual current
            # brightness, not a hardcoded default - otherwise a reading that happens to fall in
            # the assumed bucket's range never triggers an update, even if the real brightness
            # doesn't match that bucket's target at all.
            self.current_pct = current_pct
            self.current_bucket_index = nearest_bucket_index_for_pct(self.config.buckets, current_pct)
            log(f"Current monitor brightness: {current_pct}% (Bucket: {self.current_bucket_index + 1})")

    def handle_reading(self, raw_lux: float) -> None:
        """One lux reading: filter, pick a bucket, detect manual changes, apply brightness."""
        if self.median_filter.sample_count == 0:
            log("Connected, waiting for lux values.")
        self._sensor_connected = True
        self._last_error = None

        # PowerDevil may have started after us (login race) - upgrade to it when it shows
        # up, re-anchoring our tracked brightness to whatever it reports.
        adopted_pct = self.monitor.maybe_adopt_powerdevil(self.current_pct)
        if adopted_pct is not None:
            self.current_pct = adopted_pct
            self.current_bucket_index = nearest_bucket_index_for_pct(self.config.buckets, self.current_pct)

        self._raw_lux = raw_lux
        smoothed_lux = self.median_filter.add_reading(raw_lux)
        self._median_lux = smoothed_lux
        target_bucket_index = select_bucket_index(self.config.buckets, smoothed_lux, self.current_bucket_index)
        target_bucket_pct = self.config.buckets[target_bucket_index].brightness_pct

        log(
            f"Raw: {raw_lux:.1f} lx | Median: {smoothed_lux:.1f} lx "
            f"| Bucket: {target_bucket_index + 1} ({target_bucket_pct}%) "
            f"| Brightness: {self.current_pct}% "
            f"| Offset: {self.override_guard.offset_pct:+d}%"
        )

        actual_pct = self.override_guard.poll_actual(self.current_pct)

        if actual_pct is not None:
            # The monitor diverged from what we track. On the ddcutil fallback this is
            # often PowerDevil taking over at login (its DDC write and its D-Bus
            # registration are the same arrival event), not the user - so re-check for
            # PowerDevil right now, before recording a manual override, and adopt it if
            # present, re-anchoring to its value instead of blaming the user.
            adopted_pct = self.monitor.maybe_adopt_powerdevil(self.current_pct, force=True)
            if adopted_pct is not None:
                self.current_pct = adopted_pct
                self.current_bucket_index = nearest_bucket_index_for_pct(self.config.buckets, self.current_pct)
                return

            # Genuinely external - treat as a manual override.
            self.override_guard.record_override(actual_pct, target_bucket_pct)
            self.current_pct = actual_pct
            # Anchor to the ambient-selected bucket, not the bucket nearest the manual
            # brightness: the offset now carries the manual delta relative to this bucket,
            # and re-anchoring to a different bucket would double-count that delta.
            self.current_bucket_index = target_bucket_index
            self._target_dirty = False  # the user's own value is the target now
            log(
                f"Manual brightness change: {self.current_pct}% (Offset: {self.override_guard.offset_pct:+d}%) \n"
                f"Pausing auto-adjustment ({self.config.manual_override_cooldown_seconds:.0f}s)"
            )
            notify(f"Manual brightness change to {self.current_pct}%. \n Pausing auto-adjustment for {(self.config.manual_override_cooldown_seconds / 60):.0f} Minutes.", self.config)
            return

        if target_bucket_index == self.current_bucket_index and not self._target_dirty:
            return
        if self._paused or self.override_guard.active():
            return
        if not self.update_gate.enough_time_passed():
            return

        target_pct = max(
            self.config.min_brightness_pct,
            min(100, target_bucket_pct + self.override_guard.offset_pct),
        )

        try:
            self.monitor.ramp_to(self.current_pct, target_pct)
            self.current_pct = target_pct
            self.current_bucket_index = target_bucket_index
            self._target_dirty = False
            self.update_gate.mark_applied()
            log(f"Brightness set: {target_pct}% (at {smoothed_lux:.1f} lx)")
            if not self.monitor.shows_native_osd:
                notify(f"Brightness: {target_pct}% ({smoothed_lux:.1f} lx)", self.config)
        except RuntimeError as error:
            self._last_error = str(error)
            log(f"ERROR while setting brightness ({target_pct}%): {error}")
            notify(f"Error setting brightness: {str(error)[:80]}", self.config)

    def _sleep_draining(self, seconds: float) -> None:
        """Waits, but keeps answering control commands - otherwise a 'restart' or a fixed
        sensor URL would sit unapplied for the whole reconnect delay."""
        deadline = time.monotonic() + seconds
        while True:
            self._drain_commands()
            if self._stop_requested:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(COMMAND_DRAIN_SLICE_SECONDS, remaining))

    def run(self) -> None:
        log("Lunos starting...")
        self.anchor_to_monitor()
        self._publish_snapshot()

        while not self._stop_requested:
            try:
                self._reconnect_requested = False
                log(f"Connecting to sensor at {self.config.sensor_url} ...")
                for raw_lux in read_ambient_lux_values(self.config):
                    self._drain_commands()
                    if self._stop_requested:
                        break
                    if self._reconnect_requested:
                        log("Sensor settings changed, reconnecting.")
                        break
                    self.handle_reading(raw_lux)
                    self._publish_snapshot()
            except Exception as error:
                self._sensor_connected = False
                self._last_error = str(error)
                self._publish_snapshot()
                log(f"Sensor connection lost: {error}, retry in {self.config.reconnect_delay_seconds}s")
                self._sleep_draining(self.config.reconnect_delay_seconds)


def run(config: Config) -> None:
    Daemon(config).run()


if __name__ == "__main__":
    import control

    daemon = Daemon(load_config())
    server = control.serve(daemon)  # optional: the daemon runs on without a control socket
    try:
        daemon.run()
    finally:
        if server is not None:
            # Unlink the socket on the way out, so the next start doesn't have to
            # decide whether a leftover file is stale or a live second instance.
            server.server_close()
