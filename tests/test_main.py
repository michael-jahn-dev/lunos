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

import json
import tempfile
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
    """Backend double that just records every set_pct it receives."""

    def __init__(self, supports_ramping: bool):
        self.supports_ramping = supports_ramping
        self.writes: list[int] = []

    def set_pct(self, pct: int) -> None:
        self.writes.append(pct)

    def get_current_pct(self) -> int | None:
        return self.writes[-1] if self.writes else None


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

    def test_adopts_powerdevil_and_syncs_tracked_brightness(self):
        controller = self._fallback_controller()
        fake_powerdevil = RecordingBackend(supports_ramping=False)
        with mock.patch.object(main.PowerDevilBackend, "detect", return_value=fake_powerdevil):
            self.assertTrue(controller.maybe_adopt_powerdevil(current_pct=5))
        self.assertIs(controller.backend, fake_powerdevil)
        # The sync write is the fix for the stale-cache jump (manual +5% stepping
        # from PowerDevil's remembered 40% instead of the real 5%).
        self.assertEqual(fake_powerdevil.writes, [5])
        self.assertTrue(controller.shows_native_osd)

    def test_no_switch_while_powerdevil_still_absent(self):
        controller = self._fallback_controller()
        with mock.patch.object(main.PowerDevilBackend, "detect", return_value=None):
            self.assertFalse(controller.maybe_adopt_powerdevil(current_pct=5))
        self.assertIsInstance(controller.backend, main.DdcutilBackend)
        self.assertFalse(controller.shows_native_osd)

    def test_redetect_is_rate_limited(self):
        controller = self._fallback_controller(powerdevil_redetect_interval_seconds=10_000.0)
        with mock.patch.object(main.PowerDevilBackend, "detect") as detect:
            # Interval seeded at construction time, so the first re-check is still too soon.
            self.assertFalse(controller.maybe_adopt_powerdevil(current_pct=5))
        detect.assert_not_called()

    def test_never_redetects_when_powerdevil_not_preferred(self):
        controller = self._fallback_controller(prefer_powerdevil=False)
        with mock.patch.object(main.PowerDevilBackend, "detect") as detect:
            self.assertFalse(controller.maybe_adopt_powerdevil(current_pct=5))
        detect.assert_not_called()

    def test_stops_redetecting_after_adoption(self):
        controller = self._fallback_controller()
        fake_powerdevil = RecordingBackend(supports_ramping=False)
        with mock.patch.object(main.PowerDevilBackend, "detect", return_value=fake_powerdevil):
            controller.maybe_adopt_powerdevil(current_pct=5)
        with mock.patch.object(main.PowerDevilBackend, "detect") as detect:
            self.assertFalse(controller.maybe_adopt_powerdevil(current_pct=5))
        detect.assert_not_called()

    def test_failed_sync_write_still_switches_backend(self):
        controller = self._fallback_controller()

        class FailingBackend:
            supports_ramping = False

            def set_pct(self, pct: int) -> None:
                raise RuntimeError("SetBrightness failed")

            def get_current_pct(self) -> int | None:
                return None

        failing = FailingBackend()
        with mock.patch.object(main.PowerDevilBackend, "detect", return_value=failing):
            self.assertTrue(controller.maybe_adopt_powerdevil(current_pct=5))
        self.assertIs(controller.backend, failing)


# --------------------------------------------------------------------------- #
# Bucket named-tuple sanity
# --------------------------------------------------------------------------- #

class TestBucket(unittest.TestCase):
    def test_named_and_positional_access_agree(self):
        b = Bucket(15, 100, 35)
        self.assertEqual((b.min_lux, b.max_lux, b.brightness_pct), (b[0], b[1], b[2]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
