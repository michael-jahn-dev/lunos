#!/usr/bin/env python3
"""
Unit tests for Lunos's core logic.

Run with the project venv (needs `requests`/`sseclient`, which main.py imports):

    venv/bin/python3 -m unittest test_main -v
    venv/bin/python3 test_main.py            # same thing, unittest.main()

These cover the pure decision logic (bucket curve, median filter) and the
stateful helpers (manual-override guard, ramp) with fakes instead of real
hardware, so no monitor, sensor, busctl, or ddcutil is required.
"""

from __future__ import annotations

import atexit
import itertools
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import main
from main import (
    Bucket,
    BrightnessUpdateGate,
    Config,
    LuxMedianFilter,
    ManualOverrideGuard,
    MonitorController,
    nearest_bucket_index_for_pct,
    select_bucket_index,
)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #

class FakeMonitor:
    """Stands in for MonitorController: hands out scripted brightness readings."""

    def __init__(self, *readings: int | None):
        self._readings = list(readings)

    def get_current_brightness_pct(self) -> int | None:
        # Repeat the last reading once the script is exhausted, so a test can poll
        # more times than it bothered to script.
        return self._readings.pop(0) if len(self._readings) > 1 else self._readings[0]


class RecordingBackend:
    """Backend double that records every set_pct and reports a current brightness.

    `current` is what it reports before anything is written (e.g. PowerDevil's own
    remembered brightness, already applied when Lunos adopts it); once written, the
    last write wins.
    """

    def __init__(self, supports_ramping: bool, current: int | None = None):
        self.supports_ramping = supports_ramping
        self.writes: list[int] = []
        self._current = current

    def set_pct(self, pct: int) -> None:
        self.writes.append(pct)

    def get_current_pct(self) -> int | None:
        return self.writes[-1] if self.writes else self._current

    def set_config(self, config: Config) -> None:
        self.config = config


class FakeMonitorController:
    """Stands in for MonitorController inside a Daemon: no backend, no subprocesses."""

    shows_native_osd = False
    backend_name = "ddcutil"
    powerdevil_pending = False

    def __init__(self, current: int | None = None, adopts: int | None = None):
        self.current = current
        self.writes: list[int] = []
        self.configs: list[Config] = []
        self._adopts = adopts  # value a simulated PowerDevil adoption re-anchors to

    def get_current_brightness_pct(self) -> int | None:
        return self.current

    def maybe_adopt_powerdevil(self, current_pct: int, force: bool = False) -> int | None:
        adopted, self._adopts = self._adopts, None
        return adopted

    def ramp_to(self, from_pct: int, to_pct: int) -> None:
        self.writes.append(to_pct)
        self.current = to_pct

    def set_config(self, config: Config) -> int | None:
        self.configs.append(config)
        return None


# Throwaway config file for every Daemon built here. A Daemon with config_path=None
# persists set_config to config_file_path(None) - i.e. the *user's real*
# ~/.config/lunos/config.json. A test suite must never write there: it would hand a
# running daemon this file's fixture values (a one-bucket curve, notifications off)
# the next time it started.
_TEST_CONFIG_DIR = tempfile.mkdtemp(prefix="lunos-tests-")
_TEST_CONFIG_COUNTER = itertools.count()
atexit.register(shutil.rmtree, _TEST_CONFIG_DIR, True)


def make_daemon(monitor: FakeMonitorController | None = None, **overrides) -> main.Daemon:
    """A Daemon wired to a fake monitor, with persistence and notifications off and
    the rate gate open, so tests exercise decisions rather than side effects."""
    overrides.setdefault("offset_state_file", None)
    overrides.setdefault("notifications_enabled", False)
    overrides.setdefault("min_seconds_between_updates", 0.0)
    overrides.setdefault("override_poll_interval_seconds", 10_000.0)  # no manual-change polling unless asked
    monitor = monitor or FakeMonitorController(current=20)
    config_path = Path(_TEST_CONFIG_DIR) / f"config-{next(_TEST_CONFIG_COUNTER)}.json"
    with mock.patch.object(main, "MonitorController", return_value=monitor):
        daemon = main.Daemon(replace(Config(), **overrides), config_path=config_path)
    daemon.anchor_to_monitor()
    return daemon


# --------------------------------------------------------------------------- #
# Bucketed lux -> brightness curve
# --------------------------------------------------------------------------- #

class TestNearestBucketForPct(unittest.TestCase):
    BUCKETS = Config().buckets  # targets: 5, 20, 35, 50, 65, 80, 100

    def test_exact_target_matches_its_own_bucket(self):
        self.assertEqual(nearest_bucket_index_for_pct(self.BUCKETS, 50), 3)

    def test_rounds_to_closer_neighbour(self):
        # 40% sits between 35% (idx 2) and 50% (idx 3); 35 is closer.
        self.assertEqual(nearest_bucket_index_for_pct(self.BUCKETS, 40), 2)
        # 45% is closer to 50% (idx 3).
        self.assertEqual(nearest_bucket_index_for_pct(self.BUCKETS, 45), 3)

    def test_clamps_to_ends(self):
        self.assertEqual(nearest_bucket_index_for_pct(self.BUCKETS, 0), 0)
        self.assertEqual(nearest_bucket_index_for_pct(self.BUCKETS, 100), 6)


class TestSelectBucketIndex(unittest.TestCase):
    BUCKETS = Config().buckets

    def test_below_all_ranges_returns_first(self):
        self.assertEqual(select_bucket_index(self.BUCKETS, -5.0, current_index=3), 0)

    def test_above_all_ranges_returns_last(self):
        self.assertEqual(select_bucket_index(self.BUCKETS, 5000.0, current_index=0), 6)

    def test_hysteresis_stays_in_current_bucket_within_overlap(self):
        # ~25 lx falls inside BOTH bucket 1 ([5,50]) and bucket 2 ([15,100]).
        # The overlap is the hysteresis: whichever bucket we're in, we stay.
        self.assertEqual(select_bucket_index(self.BUCKETS, 25.0, current_index=1), 1)
        self.assertEqual(select_bucket_index(self.BUCKETS, 25.0, current_index=2), 2)

    def test_moves_to_containing_bucket_when_current_no_longer_contains(self):
        # 25 lx is not in bucket 5 ([250,650]); nearest containing bucket to idx 5 is 2.
        self.assertEqual(select_bucket_index(self.BUCKETS, 25.0, current_index=5), 2)

    def test_single_containing_bucket_is_selected(self):
        # 5 lx is only in bucket 0 ([0,10]); bucket 1 starts at 5 too -> both contain it.
        # Use 2 lx which is only in bucket 0.
        self.assertEqual(select_bucket_index(self.BUCKETS, 2.0, current_index=4), 0)


# --------------------------------------------------------------------------- #
# Lux median filter
# --------------------------------------------------------------------------- #

class TestLuxMedianFilter(unittest.TestCase):
    def _filter(self, window: int) -> LuxMedianFilter:
        return LuxMedianFilter(replace(Config(), median_window=window))

    def test_single_spike_is_suppressed(self):
        f = self._filter(3)
        f.add_reading(20.0)
        f.add_reading(21.0)
        # A lone 900 lx spike must not become the output; median of (20,21,900)=21.
        self.assertEqual(f.add_reading(900.0), 21.0)

    def test_tracks_a_sustained_change(self):
        f = self._filter(3)
        for v in (20.0, 21.0, 22.0):
            f.add_reading(v)
        # Once the window fills with the new level, the median follows it.
        f.add_reading(50.0)
        f.add_reading(51.0)
        self.assertEqual(f.add_reading(52.0), 51.0)

    def test_partial_window_uses_available_samples(self):
        f = self._filter(5)
        self.assertEqual(f.add_reading(30.0), 30.0)  # first sample is its own median
        self.assertEqual(f.sample_count, 1)

    def test_window_is_bounded(self):
        f = self._filter(3)
        for v in range(10):
            f.add_reading(float(v))
        self.assertEqual(f.sample_count, 3)


# --------------------------------------------------------------------------- #
# Brightness update rate gate
# --------------------------------------------------------------------------- #

class TestBrightnessUpdateGate(unittest.TestCase):
    def test_blocks_right_after_applying(self):
        gate = BrightnessUpdateGate(replace(Config(), min_seconds_between_updates=10_000.0))
        gate.mark_applied()
        self.assertFalse(gate.enough_time_passed())  # too soon

    def test_allows_after_interval(self):
        gate = BrightnessUpdateGate(replace(Config(), min_seconds_between_updates=0.0))
        gate.mark_applied()
        self.assertTrue(gate.enough_time_passed())


# --------------------------------------------------------------------------- #
# Manual override guard
# --------------------------------------------------------------------------- #

class TestManualOverrideGuard(unittest.TestCase):
    def _guard(self, monitor, **overrides) -> ManualOverrideGuard:
        # poll interval 0 so every check() actually polls, tolerance 3 as in defaults.
        # Persistence off so these tests never touch the developer's real state file.
        overrides.setdefault("offset_state_file", None)
        cfg = replace(Config(), override_poll_interval_seconds=0.0, **overrides)
        return ManualOverrideGuard(cfg, monitor)

    def test_no_change_within_tolerance_is_ignored(self):
        guard = self._guard(FakeMonitor(38))  # tracked 35, diff 3 == tolerance -> not a change
        self.assertIsNone(guard.check(tracked_pct=35, ambient_target_pct=35))
        self.assertEqual(guard.offset_pct, 0)
        self.assertFalse(guard.active())

    def test_change_beyond_tolerance_is_detected(self):
        guard = self._guard(FakeMonitor(40))  # tracked 35, diff 5 > tolerance
        result = guard.check(tracked_pct=35, ambient_target_pct=35)
        self.assertEqual(result, 40)
        self.assertTrue(guard.active())

    def test_offset_is_measured_against_ambient_target(self):
        guard = self._guard(FakeMonitor(45))
        guard.check(tracked_pct=35, ambient_target_pct=35)
        self.assertEqual(guard.offset_pct, 10)  # 45 - 35

    def test_offset_is_monotonic_regression(self):
        """
        Regression for the offset-sign bug: at a fixed ambient bucket (35%),
        raising brightness 40 -> 45 must raise the stored offset (+5 -> +10),
        not flip it (+5 -> -5) as the old nearest-bucket reference did.
        """
        guard = self._guard(FakeMonitor(40, 45))
        guard.check(tracked_pct=35, ambient_target_pct=35)
        first = guard.offset_pct
        guard.check(tracked_pct=40, ambient_target_pct=35)
        second = guard.offset_pct
        self.assertEqual((first, second), (5, 10))
        self.assertGreater(second, first)

    def test_poll_is_rate_limited(self):
        # Default 3s interval: the guard was just constructed, so a check now is too soon.
        guard = ManualOverrideGuard(replace(Config(), offset_state_file=None), FakeMonitor(40))
        self.assertIsNone(guard.check(tracked_pct=35, ambient_target_pct=35))

    def test_unreadable_brightness_is_ignored(self):
        guard = self._guard(FakeMonitor(None))
        self.assertIsNone(guard.check(tracked_pct=35, ambient_target_pct=35))
        self.assertFalse(guard.active())

    def test_cooldown_expires(self):
        guard = self._guard(FakeMonitor(40), manual_override_cooldown_seconds=0.0)
        guard.check(tracked_pct=35, ambient_target_pct=35)
        # A zero-length cooldown is already in the past.
        self.assertFalse(guard.active())

    def test_poll_actual_detects_without_side_effects(self):
        # Detection alone must not record an offset or start a cooldown - that's the caller's
        # call (the change might be PowerDevil taking over, not the user).
        guard = self._guard(FakeMonitor(45))
        self.assertEqual(guard.poll_actual(tracked_pct=35), 45)
        self.assertEqual(guard.offset_pct, 0)
        self.assertFalse(guard.active())

    def test_poll_actual_returns_none_within_tolerance(self):
        guard = self._guard(FakeMonitor(38))  # diff 3 == tolerance
        self.assertIsNone(guard.poll_actual(tracked_pct=35))

    def test_record_override_sets_offset_and_cooldown(self):
        guard = self._guard(FakeMonitor(45))
        guard.record_override(actual_pct=45, ambient_target_pct=35)
        self.assertEqual(guard.offset_pct, 10)
        self.assertTrue(guard.active())


class TestOffsetPersistence(unittest.TestCase):
    """The manual offset survives restarts via the offset_state_file; the cooldown doesn't."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_file = Path(self._tmp.name) / "offset.json"

    def _guard(self, monitor, **overrides) -> ManualOverrideGuard:
        overrides.setdefault("offset_state_file", str(self.state_file))
        cfg = replace(Config(), override_poll_interval_seconds=0.0, **overrides)
        return ManualOverrideGuard(cfg, monitor)

    def test_offset_survives_restart(self):
        first_run = self._guard(FakeMonitor(45))
        first_run.check(tracked_pct=35, ambient_target_pct=35)  # manual change -> offset +10
        self.assertEqual(first_run.offset_pct, 10)

        second_run = self._guard(FakeMonitor(45))  # fresh guard = restarted daemon
        self.assertEqual(second_run.offset_pct, 10)
        # Cooldown is a reaction to a moment, not a preference - it must NOT survive.
        self.assertFalse(second_run.active())

    def test_next_manual_change_replaces_persisted_offset(self):
        self._guard(FakeMonitor(45)).check(tracked_pct=35, ambient_target_pct=35)  # +10
        restarted = self._guard(FakeMonitor(30))
        restarted.check(tracked_pct=35, ambient_target_pct=35)  # -5
        self.assertEqual(self._guard(FakeMonitor(30)).offset_pct, -5)

    def test_missing_file_starts_at_zero(self):
        self.assertEqual(self._guard(FakeMonitor(35)).offset_pct, 0)
        self.assertFalse(self.state_file.exists())  # nothing saved until a change happens

    def test_corrupt_file_starts_at_zero(self):
        self.state_file.write_text("{not json")
        self.assertEqual(self._guard(FakeMonitor(35)).offset_pct, 0)

    def test_wrong_shape_starts_at_zero(self):
        self.state_file.write_text(json.dumps({"offset_pct": "ten"}))
        self.assertEqual(self._guard(FakeMonitor(35)).offset_pct, 0)

    def test_nonsensical_persisted_value_is_clamped(self):
        self.state_file.write_text(json.dumps({"offset_pct": 400}))
        self.assertEqual(self._guard(FakeMonitor(35)).offset_pct, 99)

    def test_none_path_disables_persistence(self):
        guard = self._guard(FakeMonitor(45), offset_state_file=None)
        guard.check(tracked_pct=35, ambient_target_pct=35)
        self.assertFalse(self.state_file.exists())


# --------------------------------------------------------------------------- #
# Ramping
# --------------------------------------------------------------------------- #

class TestRampTo(unittest.TestCase):
    def _controller(self, backend) -> MonitorController:
        # Build a controller (backend detection is side-effect-free when nothing is
        # present / falls back to ddcutil without calling it), then swap in the fake.
        controller = MonitorController(
            replace(Config(), transition_step_delay_seconds=0.0, prefer_powerdevil=False)
        )
        controller.backend = backend
        return controller

    def test_no_op_when_delta_is_zero(self):
        backend = RecordingBackend(supports_ramping=True)
        self._controller(backend).ramp_to(50, 50)
        self.assertEqual(backend.writes, [])

    def test_non_ramping_backend_writes_target_once(self):
        backend = RecordingBackend(supports_ramping=False)
        self._controller(backend).ramp_to(20, 100)
        self.assertEqual(backend.writes, [100])

    def test_small_change_is_a_single_step(self):
        backend = RecordingBackend(supports_ramping=True)
        self._controller(backend).ramp_to(35, 40)  # delta 5 < granularity 15
        self.assertEqual(backend.writes, [40])

    def test_large_jump_is_capped_and_lands_on_target(self):
        backend = RecordingBackend(supports_ramping=True)
        self._controller(backend).ramp_to(20, 100)  # ideal 6 steps, capped at 4
        self.assertEqual(len(backend.writes), 4)
        self.assertEqual(backend.writes, [40, 60, 80, 100])
        self.assertEqual(backend.writes[-1], 100)  # always reaches the target exactly

    def test_ramps_downward_too(self):
        backend = RecordingBackend(supports_ramping=True)
        self._controller(backend).ramp_to(100, 20)
        self.assertEqual(backend.writes[-1], 20)
        self.assertTrue(all(a > b for a, b in zip(backend.writes, backend.writes[1:])))


# --------------------------------------------------------------------------- #
# Late PowerDevil adoption (login race)
# --------------------------------------------------------------------------- #

class TestMaybeAdoptPowerDevil(unittest.TestCase):
    """
    Regression for the login race: systemd can start Lunos before PowerDevil has
    registered on D-Bus, so the one-shot startup detection falls back to ddcutil
    for the whole run. The controller must keep re-checking and switch over (and
    sync the tracked brightness into PowerDevil) once it appears.
    """

    def _fallback_controller(self, **overrides) -> MonitorController:
        # Detection finds nothing at startup -> controller lands on the ddcutil fallback.
        overrides.setdefault("powerdevil_redetect_interval_seconds", 0.0)
        cfg = replace(Config(), **overrides)
        with mock.patch.object(main.PowerDevilBackend, "detect", return_value=None):
            return MonitorController(cfg)

    def test_adopts_powerdevils_value_when_lunos_has_not_written(self):
        # Boot case: Lunos never applied a brightness itself, so its tracked value (5) is only
        # the power-on reading it observed. PowerDevil, the authority on Plasma, already holds
        # its own remembered brightness (40). Adoption must re-anchor to PowerDevil's 40, not
        # push the stale 5 onto it - and record no false manual override.
        controller = self._fallback_controller()
        fake_powerdevil = RecordingBackend(supports_ramping=False, current=40)
        with mock.patch.object(main.PowerDevilBackend, "detect", return_value=fake_powerdevil):
            self.assertEqual(controller.maybe_adopt_powerdevil(current_pct=5), 40)
        self.assertIs(controller.backend, fake_powerdevil)
        self.assertEqual(fake_powerdevil.writes, [])  # no push - we adopt its value
        self.assertTrue(controller.shows_native_osd)

    def test_pushes_tracked_value_when_lunos_authored_it(self):
        # Once Lunos has applied a ddcutil write, its tracked value is the real one and
        # PowerDevil's cache may be stale, so adoption pushes ours (the stale-cache fix:
        # a later manual +5% steps from 5 -> 10, not from PowerDevil's remembered 40 -> 45).
        controller = self._fallback_controller()
        controller._applied_write = True
        fake_powerdevil = RecordingBackend(supports_ramping=False, current=40)
        with mock.patch.object(main.PowerDevilBackend, "detect", return_value=fake_powerdevil):
            self.assertEqual(controller.maybe_adopt_powerdevil(current_pct=5), 5)
        self.assertEqual(fake_powerdevil.writes, [5])

    def test_no_switch_while_powerdevil_still_absent(self):
        controller = self._fallback_controller()
        with mock.patch.object(main.PowerDevilBackend, "detect", return_value=None):
            self.assertIsNone(controller.maybe_adopt_powerdevil(current_pct=5))
        self.assertIsInstance(controller.backend, main.DdcutilBackend)
        self.assertFalse(controller.shows_native_osd)

    def test_redetect_is_rate_limited(self):
        controller = self._fallback_controller(powerdevil_redetect_interval_seconds=10_000.0)
        with mock.patch.object(main.PowerDevilBackend, "detect") as detect:
            # Interval seeded at construction time, so the first re-check is still too soon.
            self.assertIsNone(controller.maybe_adopt_powerdevil(current_pct=5))
        detect.assert_not_called()

    def test_force_bypasses_rate_limit(self):
        # A mismatch on the fallback forces an immediate re-check regardless of the interval.
        controller = self._fallback_controller(powerdevil_redetect_interval_seconds=10_000.0)
        fake_powerdevil = RecordingBackend(supports_ramping=False, current=40)
        with mock.patch.object(main.PowerDevilBackend, "detect", return_value=fake_powerdevil):
            self.assertEqual(controller.maybe_adopt_powerdevil(current_pct=5, force=True), 40)
        self.assertIs(controller.backend, fake_powerdevil)

    def test_never_redetects_when_powerdevil_not_preferred(self):
        controller = self._fallback_controller(prefer_powerdevil=False)
        with mock.patch.object(main.PowerDevilBackend, "detect") as detect:
            self.assertIsNone(controller.maybe_adopt_powerdevil(current_pct=5, force=True))
        detect.assert_not_called()

    def test_stops_redetecting_after_adoption(self):
        controller = self._fallback_controller()
        fake_powerdevil = RecordingBackend(supports_ramping=False, current=40)
        with mock.patch.object(main.PowerDevilBackend, "detect", return_value=fake_powerdevil):
            controller.maybe_adopt_powerdevil(current_pct=5)
        with mock.patch.object(main.PowerDevilBackend, "detect") as detect:
            self.assertIsNone(controller.maybe_adopt_powerdevil(current_pct=5, force=True))
        detect.assert_not_called()

    def test_failed_push_still_switches_backend(self):
        controller = self._fallback_controller()
        controller._applied_write = True  # so adoption takes the push path

        class FailingBackend:
            supports_ramping = False

            def set_pct(self, pct: int) -> None:
                raise RuntimeError("SetBrightness failed")

            def get_current_pct(self) -> int | None:
                return None

        failing = FailingBackend()
        with mock.patch.object(main.PowerDevilBackend, "detect", return_value=failing):
            self.assertEqual(controller.maybe_adopt_powerdevil(current_pct=5), 5)
        self.assertIs(controller.backend, failing)


# --------------------------------------------------------------------------- #
# Bucket named-tuple sanity
# --------------------------------------------------------------------------- #

class TestBucket(unittest.TestCase):
    def test_named_and_positional_access_agree(self):
        b = Bucket(15, 100, 35)
        self.assertEqual((b.min_lux, b.max_lux, b.brightness_pct), (b[0], b[1], b[2]))


# --------------------------------------------------------------------------- #
# Config file overlay
# --------------------------------------------------------------------------- #

class TestFieldSpecs(unittest.TestCase):
    def test_every_config_field_has_a_spec(self):
        # A field without a spec can't be validated, shown in the settings window, or
        # classified in the apply matrix - it would silently be un-configurable.
        from dataclasses import fields as dataclass_fields

        self.assertEqual(
            {f.name for f in dataclass_fields(Config)},
            set(main.FIELD_SPECS),
        )

    def test_apply_classes_are_known(self):
        for name, spec in main.FIELD_SPECS.items():
            self.assertIn(spec.apply, ("hot", "reconnect", "restart"), name)


class TestConfigValidation(unittest.TestCase):
    def test_unknown_field_is_rejected(self):
        with self.assertRaises(main.ConfigFieldError):
            main.coerce_config_value("rm_rf", "yes")

    def test_type_mismatch_is_rejected(self):
        with self.assertRaises(main.ConfigFieldError):
            main.coerce_config_value("median_window", "three")
        with self.assertRaises(main.ConfigFieldError):
            main.coerce_config_value("notifications_enabled", 1)

    def test_bool_is_not_accepted_as_a_number(self):
        # bool is an int subclass, so this would otherwise sail through as 1.
        with self.assertRaises(main.ConfigFieldError):
            main.coerce_config_value("override_poll_interval_seconds", True)

    def test_range_is_enforced(self):
        with self.assertRaises(main.ConfigFieldError):
            main.coerce_config_value("median_window", 0)
        with self.assertRaises(main.ConfigFieldError):
            main.coerce_config_value("min_brightness_pct", 101)
        # A zero poll interval would turn the loop into a ddcutil spin.
        with self.assertRaises(main.ConfigFieldError):
            main.coerce_config_value("override_poll_interval_seconds", 0)

    def test_non_finite_number_is_rejected(self):
        with self.assertRaises(main.ConfigFieldError):
            main.coerce_config_value("reconnect_delay_seconds", float("inf"))

    def test_url_must_be_http(self):
        self.assertEqual(
            main.coerce_config_value("sensor_url", "http://sensor.local/events"),
            "http://sensor.local/events",
        )
        with self.assertRaises(main.ConfigFieldError):
            main.coerce_config_value("sensor_url", "file:///etc/passwd")
        with self.assertRaises(main.ConfigFieldError):
            main.coerce_config_value("sensor_url", "lunarsensor.local")

    def test_optional_fields_accept_empty_as_none(self):
        self.assertIsNone(main.coerce_config_value("monitor_display", ""))
        self.assertIsNone(main.coerce_config_value("offset_state_file", None))

    def test_required_field_rejects_empty(self):
        with self.assertRaises(main.ConfigFieldError):
            main.coerce_config_value("sensor_event_id", "  ")
        with self.assertRaises(main.ConfigFieldError):
            main.coerce_config_value("median_window", None)

    def test_buckets_round_trip_from_triples(self):
        buckets = main.coerce_config_value("buckets", [[0, 10, 5], [5, 60, 30]])
        self.assertEqual(buckets, (Bucket(0.0, 10.0, 5), Bucket(5.0, 60.0, 30)))

    def test_buckets_reject_inverted_range(self):
        with self.assertRaises(main.ConfigFieldError):
            main.coerce_config_value("buckets", [[50, 10, 5]])

    def test_buckets_reject_non_ascending_brightness(self):
        # select_bucket_index treats a lower index as darker; a descending table would
        # make the hysteresis pick nonsense.
        with self.assertRaises(main.ConfigFieldError):
            main.coerce_config_value("buckets", [[0, 10, 40], [5, 60, 20]])

    def test_buckets_reject_out_of_range_brightness(self):
        with self.assertRaises(main.ConfigFieldError):
            main.coerce_config_value("buckets", [[0, 10, 0]])
        with self.assertRaises(main.ConfigFieldError):
            main.coerce_config_value("buckets", [[0, 10, 120]])

    def test_buckets_reject_malformed_entries(self):
        for bad in ([], "nope", [[0, 10]], [[0, 10, 5, 7]], [["a", "b", "c"]]):
            with self.assertRaises(main.ConfigFieldError):
                main.coerce_config_value("buckets", bad)

    def test_validate_batch_separates_good_from_bad(self):
        accepted, errors = main.validate_config_overrides(
            {"min_brightness_pct": 12, "median_window": 0, "bogus": 1}
        )
        self.assertEqual(accepted, {"min_brightness_pct": 12})
        self.assertEqual(set(errors), {"median_window", "bogus"})


class TestConfigFile(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "config.json"

    def test_missing_file_means_defaults(self):
        self.assertEqual(main.load_config(self.path), Config())

    def test_corrupt_file_falls_back_to_defaults(self):
        self.path.write_text("{not json")
        self.assertEqual(main.load_config(self.path), Config())

    def test_non_object_file_falls_back_to_defaults(self):
        self.path.write_text("[1, 2, 3]")
        self.assertEqual(main.load_config(self.path), Config())

    def test_unknown_keys_are_dropped_but_the_rest_is_kept(self):
        self.path.write_text(json.dumps({"min_brightness_pct": 12, "from_the_future": True}))
        self.assertEqual(main.load_config(self.path).min_brightness_pct, 12)

    def test_invalid_value_is_dropped_field_by_field(self):
        self.path.write_text(json.dumps({"min_brightness_pct": 12, "median_window": -1}))
        config = main.load_config(self.path)
        self.assertEqual(config.min_brightness_pct, 12)
        self.assertEqual(config.median_window, Config().median_window)

    def test_buckets_are_rebuilt_as_bucket_tuples(self):
        self.path.write_text(json.dumps({"buckets": [[0, 10, 5], [5, 60, 30]]}))
        buckets = main.load_config(self.path).buckets
        self.assertIsInstance(buckets[0], Bucket)
        self.assertEqual(buckets[1].brightness_pct, 30)

    def test_save_writes_only_overrides(self):
        main.save_config(replace(Config(), min_brightness_pct=12), self.path)
        self.assertEqual(json.loads(self.path.read_text()), {"min_brightness_pct": 12})

    def test_save_load_round_trip(self):
        config = replace(
            Config(),
            min_brightness_pct=12,
            sensor_url="https://sensor.example/events",
            monitor_display="2",
            buckets=(Bucket(0, 10, 5), Bucket(5, 60, 30)),
        )
        main.save_config(config, self.path)
        self.assertEqual(main.load_config(self.path), config)

    def test_defaults_save_as_an_empty_object(self):
        main.save_config(Config(), self.path)
        self.assertEqual(json.loads(self.path.read_text()), {})

    def test_env_var_selects_the_path(self):
        with mock.patch.dict(os.environ, {main.CONFIG_FILE_ENV_VAR: str(self.path)}):
            self.assertEqual(main.config_file_path(), self.path)

    def test_schema_reports_default_and_current(self):
        schema = {entry["name"]: entry for entry in main.config_schema(replace(Config(), median_window=9))}
        self.assertEqual(schema["median_window"]["current"], 9)
        self.assertEqual(schema["median_window"]["default"], Config().median_window)
        self.assertEqual(schema["buckets"]["current"][0], [0.0, 10.0, 5])  # serialized as triples


# --------------------------------------------------------------------------- #
# Runtime re-application of settings (the apply matrix)
# --------------------------------------------------------------------------- #

class TestComponentSetConfig(unittest.TestCase):
    def test_median_window_rebuild_keeps_the_newest_samples(self):
        f = LuxMedianFilter(replace(Config(), median_window=5))
        for value in (1.0, 2.0, 3.0, 4.0, 5.0):
            f.add_reading(value)
        f.set_config(replace(Config(), median_window=3))
        self.assertEqual(f.sample_count, 3)
        self.assertEqual(f.add_reading(9.0), 5.0)  # median of the newest (4,5,9)

    def test_update_gate_picks_up_a_new_interval(self):
        gate = BrightnessUpdateGate(replace(Config(), min_seconds_between_updates=10_000.0))
        gate.mark_applied()
        self.assertFalse(gate.enough_time_passed())
        gate.set_config(replace(Config(), min_seconds_between_updates=0.0))
        self.assertTrue(gate.enough_time_passed())

    def test_controller_forwards_the_config_to_its_backend(self):
        # monitor_display / powerdevil_show_osd are read on the backend, not the
        # controller - a swap that stopped at the controller would do nothing.
        controller = MonitorController(replace(Config(), prefer_powerdevil=False))
        controller.set_config(replace(Config(), prefer_powerdevil=False, monitor_display="2"))
        self.assertEqual(controller.backend._display_args(), ["--display", "2"])

    def test_powerdevil_osd_toggle_keeps_shows_native_osd_consistent(self):
        powerdevil = main.PowerDevilBackend("/org/kde/ScreenBrightness/x", Config())
        with mock.patch.object(main.PowerDevilBackend, "detect", return_value=powerdevil):
            controller = MonitorController(Config())
        self.assertTrue(controller.shows_native_osd)

        controller.set_config(replace(Config(), powerdevil_show_osd=False))
        self.assertFalse(controller.shows_native_osd)
        self.assertFalse(controller.backend._config.powerdevil_show_osd)

    def test_turning_powerdevil_off_reselects_the_backend_and_reanchors(self):
        powerdevil = main.PowerDevilBackend("/org/kde/ScreenBrightness/x", Config())
        with mock.patch.object(main.PowerDevilBackend, "detect", return_value=powerdevil):
            controller = MonitorController(Config())

        with mock.patch.object(main.DdcutilBackend, "get_current_pct", return_value=42):
            adopted = controller.set_config(replace(Config(), prefer_powerdevil=False))
        self.assertEqual(adopted, 42)  # the new backend's own reading is the truth
        self.assertIsInstance(controller.backend, main.DdcutilBackend)
        self.assertFalse(controller.shows_native_osd)

    def test_hot_field_change_does_not_reselect_the_backend(self):
        controller = MonitorController(replace(Config(), prefer_powerdevil=False))
        backend = controller.backend
        self.assertIsNone(controller.set_config(replace(Config(), prefer_powerdevil=False, min_brightness_pct=9)))
        self.assertIs(controller.backend, backend)

    def test_guard_follows_a_moved_state_file(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "a.json"
            second = Path(directory) / "b.json"
            guard = ManualOverrideGuard(replace(Config(), offset_state_file=str(first)), FakeMonitor(50))
            guard.set_offset(7)
            guard.set_config(replace(Config(), offset_state_file=str(second)))
            self.assertEqual(json.loads(second.read_text())["offset_pct"], 7)


# --------------------------------------------------------------------------- #
# Offset setter (tray-driven, as opposed to a detected manual change)
# --------------------------------------------------------------------------- #

class TestSetOffset(unittest.TestCase):
    def _guard(self, monitor=None, **overrides) -> ManualOverrideGuard:
        overrides.setdefault("offset_state_file", None)
        return ManualOverrideGuard(replace(Config(), **overrides), monitor or FakeMonitor(50))

    def test_clamps_to_the_sane_range(self):
        guard = self._guard()
        guard.set_offset(500)
        self.assertEqual(guard.offset_pct, 99)
        guard.set_offset(-500)
        self.assertEqual(guard.offset_pct, -99)

    def test_clears_a_running_cooldown(self):
        # An offset set in Lunos's own UI is a request to act, not to back off.
        guard = self._guard(manual_override_cooldown_seconds=10_000.0)
        guard.record_override(actual_pct=60, ambient_target_pct=50)
        self.assertTrue(guard.active())
        guard.set_offset(5)
        self.assertFalse(guard.active())
        self.assertEqual(guard.offset_pct, 5)

    def test_does_not_round_trip_through_the_monitor(self):
        # record_override would recompute the offset from actual-minus-target and hand
        # back a different number than the user picked.
        class ExplodingMonitor:
            def get_current_brightness_pct(self):
                raise AssertionError("set_offset must not read the monitor")

        guard = self._guard(monitor=ExplodingMonitor())
        guard.set_offset(-12)
        self.assertEqual(guard.offset_pct, -12)

    def test_is_persisted_and_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "offset.json"
            config = replace(Config(), offset_state_file=str(path))
            ManualOverrideGuard(config, FakeMonitor(50)).set_offset(-8)
            self.assertEqual(ManualOverrideGuard(config, FakeMonitor(50)).offset_pct, -8)

    def test_clear_override_keeps_the_offset(self):
        guard = self._guard(manual_override_cooldown_seconds=10_000.0)
        guard.record_override(actual_pct=60, ambient_target_pct=50)
        guard.clear_override()
        self.assertFalse(guard.active())
        self.assertEqual(guard.offset_pct, 10)

    def test_start_override_pauses_without_recording_an_offset(self):
        guard = self._guard()
        guard.start_override(10_000.0)
        self.assertTrue(guard.active())
        self.assertEqual(guard.offset_pct, 0)
        self.assertGreater(guard.seconds_left(), 0)


# --------------------------------------------------------------------------- #
# Daemon: command handling, apply matrix, snapshot
# --------------------------------------------------------------------------- #

class TestDaemonCommands(unittest.TestCase):
    def test_test_daemons_never_write_the_users_real_config_file(self):
        # Regression: make_daemon() once left config_path=None, so every set_config in
        # this file persisted to ~/.config/lunos/config.json and the next daemon start
        # picked up the fixtures (a one-bucket 70% curve, notifications off).
        daemon = make_daemon()
        self.assertIsNotNone(daemon._config_path)
        self.assertNotEqual(Path(daemon._config_path), main.config_file_path())
        daemon.dispatch("set_config", {"fields": {"min_brightness_pct": 9}})
        daemon._drain_commands()
        self.assertTrue(Path(daemon._config_path).exists())
        self.assertTrue(str(daemon._config_path).startswith(tempfile.gettempdir()))

    def test_snapshot_reports_live_state(self):
        daemon = make_daemon()
        daemon.handle_reading(20.0)
        daemon._publish_snapshot()
        state = daemon.snapshot()
        self.assertEqual(state["raw_lux"], 20.0)
        self.assertEqual(state["median_lux"], 20.0)
        self.assertEqual(state["backend"], "ddcutil")
        self.assertTrue(state["sensor_connected"])
        self.assertEqual(state["protocol"], main.PROTOCOL_VERSION)

    def test_commands_submitted_from_another_thread_run_on_the_loop_thread(self):
        daemon = make_daemon()
        loop_thread = threading.get_ident()
        seen: list[int] = []
        daemon.add_snapshot_listener(lambda state: seen.append(threading.get_ident()))

        worker = threading.Thread(target=lambda: daemon.dispatch("set_offset", {"offset_pct": 10}))
        worker.start()
        worker.join()
        self.assertEqual(daemon.override_guard.offset_pct, 0)  # not applied off-thread

        daemon._drain_commands()
        daemon._publish_snapshot()
        self.assertEqual(daemon.override_guard.offset_pct, 10)
        self.assertEqual(seen, [loop_thread])

    def test_offset_change_applies_without_waiting_for_a_bucket_change(self):
        monitor = FakeMonitorController(current=20)
        daemon = make_daemon(monitor)
        daemon.handle_reading(20.0)
        self.assertEqual(monitor.writes, [])  # steady light, bucket unchanged

        daemon.dispatch("set_offset", {"offset_pct": 10})
        daemon._drain_commands()
        daemon.handle_reading(20.0)
        self.assertEqual(monitor.writes, [30])  # bucket target 20% + offset

    def test_offset_is_applied_only_once(self):
        monitor = FakeMonitorController(current=20)
        daemon = make_daemon(monitor)
        daemon.dispatch("set_offset", {"offset_pct": 10})
        daemon._drain_commands()
        daemon.handle_reading(20.0)
        daemon.handle_reading(20.0)
        self.assertEqual(monitor.writes, [30])

    def test_pause_blocks_adjustment_and_resume_reapplies(self):
        monitor = FakeMonitorController(current=20)
        daemon = make_daemon(monitor)
        daemon.dispatch("pause", {})
        daemon._drain_commands()
        daemon.handle_reading(500.0)  # a bucket change that must not be applied
        self.assertEqual(monitor.writes, [])

        daemon.dispatch("resume", {})
        daemon._drain_commands()
        daemon.handle_reading(500.0)
        self.assertEqual(monitor.writes, [80])

    def test_pause_with_seconds_uses_the_override_cooldown(self):
        daemon = make_daemon()
        daemon.dispatch("pause", {"seconds": 600})
        daemon._drain_commands()
        self.assertTrue(daemon.override_guard.active())
        self.assertFalse(daemon._paused)

    def test_restart_stops_the_loop(self):
        daemon = make_daemon()
        daemon.dispatch("restart", {})
        daemon._drain_commands()
        self.assertTrue(daemon._stop_requested)

    def test_unknown_command_is_reported_not_ignored(self):
        reply = make_daemon().dispatch("drop_tables", {})
        self.assertFalse(reply["ok"])
        self.assertIn("unknown command", reply["error"])

    def test_set_offset_rejects_bad_payloads(self):
        daemon = make_daemon()
        for payload in ({}, {"offset_pct": "10"}, {"offset_pct": True}, {"offset_pct": 500}):
            self.assertFalse(daemon.dispatch("set_offset", payload)["ok"], payload)

    def test_set_config_reports_invalid_fields_and_applies_nothing(self):
        daemon = make_daemon()
        reply = daemon.dispatch("set_config", {"fields": {"min_brightness_pct": 999}})
        self.assertFalse(reply["ok"])
        self.assertIn("min_brightness_pct", reply["errors"])
        daemon._drain_commands()
        self.assertEqual(daemon.config.min_brightness_pct, Config().min_brightness_pct)

    def test_set_config_classifies_fields_by_how_they_apply(self):
        reply = make_daemon().dispatch("set_config", {"fields": {
            "min_brightness_pct": 9,
            "sensor_url": "http://other.local/events",
            "default_bucket_index": 2,
        }})
        self.assertEqual(reply["applied"], ["min_brightness_pct"])
        self.assertEqual(reply["reconnecting"], ["sensor_url"])
        self.assertEqual(reply["restart_required"], ["default_bucket_index"])

    def test_set_config_persists_the_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            daemon = make_daemon()
            daemon._config_path = path
            daemon.dispatch("set_config", {"fields": {"min_brightness_pct": 9}})
            daemon._drain_commands()
            self.assertEqual(daemon.config.min_brightness_pct, 9)
            self.assertEqual(json.loads(path.read_text())["min_brightness_pct"], 9)

    def test_reload_config_rereads_a_hand_edited_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"min_brightness_pct": 11}))
            daemon = make_daemon()
            daemon._config_path = path
            daemon.dispatch("reload_config", {})
            daemon._drain_commands()
            self.assertEqual(daemon.config.min_brightness_pct, 11)

    def test_sensor_field_change_requests_a_reconnect(self):
        daemon = make_daemon()
        daemon.apply_config(replace(daemon.config, sensor_url="http://other.local/events"))
        self.assertTrue(daemon._reconnect_requested)

    def test_hot_field_change_does_not_request_a_reconnect(self):
        daemon = make_daemon()
        daemon.apply_config(replace(daemon.config, min_brightness_pct=9))
        self.assertFalse(daemon._reconnect_requested)

    def test_new_bucket_table_reanchors_the_current_index(self):
        daemon = make_daemon(FakeMonitorController(current=80))
        self.assertEqual(daemon.current_bucket_index, 5)  # 80% -> bucket 6 of the defaults
        daemon.apply_config(replace(daemon.config, buckets=(Bucket(0, 50, 30), Bucket(20, 1000, 90))))
        # The old index 5 doesn't exist in the new two-rung table; 80% is nearest 90%.
        self.assertEqual(daemon.current_bucket_index, 1)

    def test_hot_field_is_visible_on_the_next_reading(self):
        monitor = FakeMonitorController(current=20)
        daemon = make_daemon(monitor)
        daemon.dispatch("set_config", {"fields": {"buckets": [[0, 1000, 70]]}})
        daemon._drain_commands()
        daemon.handle_reading(20.0)
        self.assertEqual(monitor.writes, [70])

    def test_min_brightness_floor_survives_a_large_negative_offset(self):
        monitor = FakeMonitorController(current=20)
        daemon = make_daemon(monitor, min_brightness_pct=5)
        daemon.dispatch("set_offset", {"offset_pct": -99})
        daemon._drain_commands()
        daemon.handle_reading(20.0)
        self.assertEqual(monitor.writes, [5])

    def test_daemon_starts_when_default_bucket_index_exceeds_the_table(self):
        # A shrunken bucket table would otherwise index out of range at startup.
        daemon = make_daemon(
            FakeMonitorController(current=None),
            buckets=(Bucket(0, 50, 30), Bucket(20, 1000, 90)),
            default_bucket_index=5,
        )
        self.assertEqual(daemon.current_bucket_index, 1)

    def test_dispatch_get_schema_reflects_the_live_config(self):
        daemon = make_daemon()
        daemon.dispatch("set_config", {"fields": {"median_window": 7}})
        daemon._drain_commands()
        schema = {entry["name"]: entry for entry in daemon.dispatch("get_schema", {})["schema"]}
        self.assertEqual(schema["median_window"]["current"], 7)

    def test_a_failing_listener_does_not_break_the_loop(self):
        daemon = make_daemon()

        def boom(state):
            raise RuntimeError("subscriber exploded")

        daemon.add_snapshot_listener(boom)
        daemon._publish_snapshot()  # must not raise

    def test_a_failing_command_does_not_break_the_loop(self):
        daemon = make_daemon()
        before = daemon.config
        daemon.submit("set_config", {"fields": {"not_a_setting": 1}})  # bypasses dispatch's validation
        daemon._drain_commands()  # must not raise
        self.assertIs(daemon.config, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
