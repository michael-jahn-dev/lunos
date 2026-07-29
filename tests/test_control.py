#!/usr/bin/env python3
"""
Unit tests for the control socket.

These drive a real AF_UNIX socket in a temporary directory - no hardware, no
sensor, no GUI. The server is deliberately decoupled from `main` (it only needs
`dispatch`/`snapshot`/`add_snapshot_listener`/`protocol_version`), so most tests
use a fake daemon; one integration test drives a real `main.Daemon`.

    venv/bin/python3 -m unittest discover -s tests -t .
"""

from __future__ import annotations

import json
import os
import queue
import socket
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import control
from tests.test_main import FakeMonitorController, make_daemon


# Shutdown latency is bounded by serve_forever's poll interval; the production
# default trades that for rare idle wakeups, which a test suite shouldn't pay for.
TEST_POLL_INTERVAL = 0.02


class FakeApp:
    """Minimal ControlTarget: records commands, hands back canned replies."""

    protocol_version = 1

    def __init__(self):
        self.commands: list[tuple[str, dict]] = []
        self.listeners: list = []
        self.state: dict = {"brightness_pct": 50, "offset_pct": 0}

    def dispatch(self, name: str, payload: dict) -> dict:
        self.commands.append((name, payload))
        return {"ok": True, "echo": name}

    def snapshot(self) -> dict:
        return dict(self.state)

    def add_snapshot_listener(self, listener) -> None:
        self.listeners.append(listener)

    def publish(self, state: dict) -> None:
        self.state = state
        for listener in self.listeners:
            listener(state)


class ControlSocketTestCase(unittest.TestCase):
    """Starts a server on a temp-directory socket and tears it down again."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "lunos" / "control.sock"
        self.app = FakeApp()
        self.server = control.serve(self.app, self.path, poll_interval=TEST_POLL_INTERVAL)
        self.assertIsNotNone(self.server, "server failed to start")
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()

    def connect(self):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5)
        client.connect(str(self.path))
        stream = client.makefile("rwb")
        self.client = client
        self.addCleanup(client.close)
        banner = json.loads(stream.readline())
        return stream, banner

    def request(self, stream, **request) -> dict:
        stream.write((json.dumps(request) + "\n").encode())
        stream.flush()
        return json.loads(stream.readline())


class TestProtocol(ControlSocketTestCase):
    def test_banner_announces_protocol_and_commands(self):
        _, banner = self.connect()
        self.assertTrue(banner["ok"])
        self.assertEqual(banner["protocol"], self.app.protocol_version)
        self.assertIn("get_state", banner["commands"])

    def test_every_reply_carries_the_protocol_version(self):
        stream, _ = self.connect()
        self.assertEqual(self.request(stream, cmd="get_state")["protocol"], 1)

    def test_command_reaches_the_daemon_with_its_payload(self):
        stream, _ = self.connect()
        reply = self.request(stream, cmd="set_offset", offset_pct=10)
        self.assertTrue(reply["ok"])
        name, payload = self.app.commands[-1]
        self.assertEqual(name, "set_offset")
        self.assertEqual(payload["offset_pct"], 10)

    def test_unknown_command_is_rejected_without_closing(self):
        stream, _ = self.connect()
        reply = self.request(stream, cmd="rm_rf")
        self.assertFalse(reply["ok"])
        self.assertIn("unknown command", reply["error"])
        self.assertTrue(self.request(stream, cmd="get_state")["ok"])  # still usable
        self.assertEqual(self.app.commands, [("get_state", {"cmd": "get_state"})])

    def test_malformed_json_is_rejected_without_closing(self):
        stream, _ = self.connect()
        stream.write(b"{not json}\n")
        stream.flush()
        reply = json.loads(stream.readline())
        self.assertFalse(reply["ok"])
        self.assertIn("malformed JSON", reply["error"])
        self.assertTrue(self.request(stream, cmd="get_state")["ok"])

    def test_non_object_request_is_rejected(self):
        stream, _ = self.connect()
        stream.write(b"[1, 2, 3]\n")
        stream.flush()
        self.assertFalse(json.loads(stream.readline())["ok"])

    def test_request_without_a_command_is_rejected(self):
        stream, _ = self.connect()
        self.assertFalse(self.request(stream, offset_pct=5)["ok"])

    def test_blank_lines_are_ignored(self):
        stream, _ = self.connect()
        stream.write(b"\n\n")
        stream.flush()
        self.assertTrue(self.request(stream, cmd="get_state")["ok"])

    def test_oversized_request_is_refused_and_the_connection_dropped(self):
        # An unbounded readline() on a socket is a memory-exhaustion bug.
        stream, _ = self.connect()
        stream.write(b'{"cmd":"get_state","pad":"' + b"x" * (control.MAX_REQUEST_BYTES + 10) + b'"}\n')
        stream.flush()
        reply = json.loads(stream.readline())
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "request too large")
        self.assertEqual(stream.readline(), b"")  # server hung up
        self.assertEqual(self.app.commands, [])

    def test_several_commands_on_one_connection(self):
        stream, _ = self.connect()
        for _ in range(3):
            self.assertTrue(self.request(stream, cmd="get_state")["ok"])
        self.assertEqual(len(self.app.commands), 3)

    def test_concurrent_clients_are_served(self):
        first, _ = self.connect()
        second, _ = self.connect()
        self.assertTrue(self.request(first, cmd="get_state")["ok"])
        self.assertTrue(self.request(second, cmd="get_state")["ok"])


class TestSubscribe(ControlSocketTestCase):
    def test_subscribe_acks_with_current_state_then_pushes(self):
        stream, _ = self.connect()
        ack = self.request(stream, cmd="subscribe")
        self.assertTrue(ack["subscribed"])
        self.assertEqual(ack["state"]["brightness_pct"], 50)

        self.app.publish({"brightness_pct": 60})
        self.app.publish({"brightness_pct": 70})
        self.assertEqual(json.loads(stream.readline())["state"]["brightness_pct"], 60)
        self.assertEqual(json.loads(stream.readline())["state"]["brightness_pct"], 70)

    def test_subscriber_is_registered_and_unregistered(self):
        stream, _ = self.connect()
        self.request(stream, cmd="subscribe")
        self.assertTrue(self._wait_for(lambda: len(self.server._subscribers) == 1))

        stream.close()
        self.client.close()

        def gone_after_a_push() -> bool:
            # A closed peer is only noticed on a write, and the first one may still
            # land in a kernel buffer - so keep publishing until the handler notices.
            self.app.publish({"brightness_pct": 60})
            return len(self.server._subscribers) == 0

        self.assertTrue(self._wait_for(gone_after_a_push))

    def test_broadcast_never_blocks_on_a_stalled_subscriber(self):
        # The loop thread publishes; a client that stopped reading must cost it nothing
        # but the oldest queued state.
        updates: queue.Queue = queue.Queue(maxsize=2)
        self.server.add_subscriber(updates)
        for value in range(5):
            self.server.broadcast({"brightness_pct": value})
        self.assertEqual(updates.qsize(), 2)
        self.assertEqual(updates.get()["brightness_pct"], 3)  # oldest dropped, newest kept
        self.assertEqual(updates.get()["brightness_pct"], 4)

    @staticmethod
    def _wait_for(condition, timeout: float = 2.0) -> bool:
        deadline = threading.Event()
        for _ in range(int(timeout / 0.02)):
            if condition():
                return True
            deadline.wait(0.02)
        return condition()


class TestSocketPath(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "lunos" / "control.sock"

    def test_default_path_uses_the_runtime_dir_subdirectory(self):
        # The subdirectory is what makes --filesystem=xdg-run/lunos possible later;
        # a bare $XDG_RUNTIME_DIR/lunos.sock could not be shared into a sandbox.
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1000"}, clear=False):
            os.environ.pop(control.SOCKET_PATH_ENV_VAR, None)
            self.assertEqual(control.socket_path(), Path("/run/user/1000/lunos/control.sock"))

    def test_falls_back_to_cache_without_a_runtime_dir(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                control.socket_path(),
                Path.home() / ".cache" / "lunos" / "control.sock",
            )

    def test_env_var_overrides_the_path(self):
        with mock.patch.dict(os.environ, {control.SOCKET_PATH_ENV_VAR: str(self.path)}):
            self.assertEqual(control.socket_path(), self.path)

    def test_directory_is_private_and_socket_is_not_world_writable(self):
        server = control.serve(FakeApp(), self.path, poll_interval=TEST_POLL_INTERVAL)
        self.addCleanup(server.server_close)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_a_too_permissive_existing_directory_is_tightened(self):
        self.path.parent.mkdir(parents=True)
        os.chmod(self.path.parent, 0o777)
        server = control.serve(FakeApp(), self.path, poll_interval=TEST_POLL_INTERVAL)
        self.addCleanup(server.server_close)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_stale_socket_file_is_replaced(self):
        self.path.parent.mkdir(parents=True)
        self.path.touch()  # left behind by a crash; nothing is listening
        server = control.serve(FakeApp(), self.path, poll_interval=TEST_POLL_INTERVAL)
        self.addCleanup(server.server_close)
        self.assertIsNotNone(server)

    def test_a_live_socket_is_not_hijacked(self):
        first = control.serve(FakeApp(), self.path, poll_interval=TEST_POLL_INTERVAL)
        self.addCleanup(first.server_close)
        self.assertIsNone(control.serve(FakeApp(), self.path, poll_interval=TEST_POLL_INTERVAL))  # logs and gives up

    def test_socket_file_is_removed_on_close(self):
        server = control.serve(FakeApp(), self.path, poll_interval=TEST_POLL_INTERVAL)
        server.server_close()
        self.assertFalse(self.path.exists())

    def test_daemon_survives_an_unusable_socket_path(self):
        # Brightness control must not depend on the GUI's IPC surface.
        unusable = Path(self._dir.name) / "not-a-directory" / "x" / "control.sock"
        (Path(self._dir.name) / "not-a-directory").write_text("")
        self.assertIsNone(control.serve(FakeApp(), unusable, poll_interval=TEST_POLL_INTERVAL))


class TestAgainstRealDaemon(unittest.TestCase):
    """One end-to-end pass over the real Daemon, so the two halves are known to fit."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.daemon = make_daemon(FakeMonitorController(current=20))
        self.server = control.serve(
            self.daemon, Path(self._dir.name) / "lunos" / "control.sock", poll_interval=TEST_POLL_INTERVAL
        )
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()

    def _connected(self):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5)
        client.connect(str(self.server.path))
        self.addCleanup(client.close)
        stream = client.makefile("rwb")
        stream.readline()  # banner
        return stream

    def _request(self, stream, **request) -> dict:
        stream.write((json.dumps(request) + "\n").encode())
        stream.flush()
        return json.loads(stream.readline())

    def test_get_state_reports_the_snapshot(self):
        self.daemon.handle_reading(20.0)
        self.daemon._publish_snapshot()
        state = self._request(self._connected(), cmd="get_state")["state"]
        self.assertEqual(state["raw_lux"], 20.0)
        self.assertEqual(state["backend"], "ddcutil")

    def test_get_schema_describes_every_setting(self):
        schema = self._request(self._connected(), cmd="get_schema")["schema"]
        self.assertEqual({entry["name"] for entry in schema}, set(__import__("main").FIELD_SPECS))

    def test_set_config_is_validated_server_side(self):
        reply = self._request(self._connected(), cmd="set_config", fields={"median_window": 0})
        self.assertFalse(reply["ok"])
        self.assertIn("median_window", reply["errors"])

    def test_offset_command_reaches_the_loop_thread(self):
        stream = self._connected()
        self.assertTrue(self._request(stream, cmd="set_offset", offset_pct=12)["ok"])
        self.daemon._drain_commands()  # stands in for the loop's next iteration
        self.assertEqual(self.daemon.override_guard.offset_pct, 12)

    def test_push_stream_follows_the_loop(self):
        stream = self._connected()
        self.assertTrue(self._request(stream, cmd="subscribe")["subscribed"])
        self.daemon.handle_reading(500.0)
        self.daemon._publish_snapshot()
        pushed = json.loads(stream.readline())["state"]
        self.assertEqual(pushed["raw_lux"], 500.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
