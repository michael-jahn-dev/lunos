#!/usr/bin/env python3
"""
Unit tests for the tray app's pure logic.

Only the parts that need no display and no Qt event loop are covered - importing
`tray` needs PySide6 (`sudo dnf install python3-pyside6`), which the daemon's venv
deliberately does not have, so the whole module is skipped when it is missing.
Everything else about the tray (SNI registration, DBusMenu rendering, the login
race) is in the manual checklist in docs/features/system-tray-app.md, because no
unit test can stand in for a real Plasma session.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # must be set before Qt initialises

try:
    from PySide6.QtWidgets import QApplication

    from tray import SingleInstance, bucket_problems
except ImportError:  # PySide6 not installed (e.g. the daemon's venv)
    QApplication = None
    SingleInstance = None
    bucket_problems = None


@unittest.skipIf(bucket_problems is None, "PySide6 not installed")
class TestBucketProblems(unittest.TestCase):
    """
    The overlap rule is the one that must not be backwards: overlapping buckets
    *are* the hysteresis, so a missing overlap is the warning and a present one is
    correct. A dialog that warned about overlap would push every user into a curve
    that visibly flickers.
    """

    DEFAULTS = [[0, 10, 5], [5, 50, 20], [15, 100, 35], [60, 300, 50]]

    def test_the_default_overlapping_curve_is_accepted_silently(self):
        errors, warnings = bucket_problems(self.DEFAULTS)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_a_gap_between_buckets_is_warned_about(self):
        errors, warnings = bucket_problems([[0, 10, 5], [20, 50, 20]])
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("do not overlap", warnings[0])

    def test_merely_touching_buckets_are_warned_about_too(self):
        # max_lux == next min_lux leaves no hysteresis band at all.
        _, warnings = bucket_problems([[0, 10, 5], [10, 50, 20]])
        self.assertEqual(len(warnings), 1)

    def test_empty_table_is_an_error(self):
        errors, _ = bucket_problems([])
        self.assertEqual(len(errors), 1)

    def test_inverted_lux_range_is_an_error(self):
        errors, _ = bucket_problems([[50, 10, 5]])
        self.assertTrue(any("min lux" in message for message in errors))

    def test_out_of_range_brightness_is_an_error(self):
        errors, _ = bucket_problems([[0, 10, 0]])
        self.assertTrue(any("between 1 and 100" in message for message in errors))
        errors, _ = bucket_problems([[0, 10, 140]])
        self.assertTrue(any("between 1 and 100" in message for message in errors))

    def test_non_ascending_brightness_is_an_error(self):
        errors, _ = bucket_problems([[0, 10, 40], [5, 50, 20]])
        self.assertTrue(any("higher than bucket" in message for message in errors))

    def test_negative_lux_is_an_error(self):
        errors, _ = bucket_problems([[-5, 10, 20]])
        self.assertTrue(any("negative" in message for message in errors))


@unittest.skipIf(SingleInstance is None, "PySide6 not installed")
class TestSingleInstance(unittest.TestCase):
    """
    Regression: this lock was originally built on "QLocalServer.listen() failed,
    therefore another instance holds the path". It does not fail - Qt replaces the
    socket file and reports success - so every launch claimed the lock and a second
    `python3 tray.py` added a second tray icon instead of raising the first window.
    The lock is an flock now; flock() from a second file descriptor is refused even
    within one process, which is exactly what this asserts.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.socket = Path(self._dir.name) / "tray.sock"
        self.lock = Path(self._dir.name) / "tray.lock"

    def _instance(self) -> SingleInstance:
        return SingleInstance(self.socket, self.lock)

    def test_first_claim_wins(self):
        first = self._instance()
        self.addCleanup(lambda: first._lock_file and first._lock_file.close())
        self.assertTrue(first.claim())

    def test_second_claim_is_refused_while_the_first_holds_the_lock(self):
        first = self._instance()
        self.addCleanup(lambda: first._lock_file and first._lock_file.close())
        self.assertTrue(first.claim())
        self.assertFalse(self._instance().claim())

    def test_lock_is_reusable_once_the_holder_lets_go(self):
        # The kernel drops an flock when the process dies, so a crashed instance
        # must not lock the user out of their own tray.
        first = self._instance()
        self.assertTrue(first.claim())
        first._lock_file.close()

        second = self._instance()
        self.addCleanup(lambda: second._lock_file and second._lock_file.close())
        self.assertTrue(second.claim())

    def test_claiming_creates_the_raise_socket(self):
        instance = self._instance()
        self.addCleanup(lambda: instance._lock_file and instance._lock_file.close())
        instance.claim()
        self.assertTrue(self.socket.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
