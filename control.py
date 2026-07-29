#!/usr/bin/env python3
"""
Lunos control socket - the daemon's IPC surface for the tray app.

A per-user `AF_UNIX` socket speaking newline-delimited JSON: one compact object
per request, one per reply. Stdlib only (`socket`/`socketserver`/`json`/
`threading`), so the daemon gains no new dependency for a path only the GUI
uses, and the protocol stays drivable by hand:

    socat - UNIX-CONNECT:$XDG_RUNTIME_DIR/lunos/control.sock
    {"cmd": "get_state"}

Access control is the socket's file permissions and nothing else - the same
trust boundary as the session bus. The directory is created 0700 and the socket
0600; anything running as this user can drive the monitor and rewrite the
config, which is why the socket lives in $XDG_RUNTIME_DIR (per-user, tmpfs,
wiped at logout) rather than /tmp.

This module deliberately imports nothing from `main`: `main.py` runs as a
script, so importing it back by name would create a second copy of the module
with its own `Config` class. Everything the server needs comes from the daemon
object it is handed (`dispatch`, `snapshot`, `add_snapshot_listener`,
`protocol_version`).
"""

from __future__ import annotations

import json
import os
import queue
import socket
import socketserver
import threading
from pathlib import Path
from typing import Any, Protocol


SOCKET_DIRECTORY_NAME = "lunos"
SOCKET_FILE_NAME = "control.sock"
SOCKET_PATH_ENV_VAR = "LUNOS_CONTROL_SOCKET"

# An unbounded readline() on a socket is a memory-exhaustion bug waiting to happen.
# A whole bucket table serializes to a couple of kB, so this is generous.
MAX_REQUEST_BYTES = 64 * 1024

# Per-subscriber backlog. A subscriber that stops reading (suspended laptop, frozen
# GUI) must not grow the daemon's memory: past this, the oldest state is dropped -
# stale state is worthless anyway, since every push is a full snapshot.
SUBSCRIBER_QUEUE_SIZE = 32

_SHUTDOWN = object()  # sentinel pushed to subscribers so their writer loops end


def log(message: str) -> None:
    # Deliberately not imported from main - see the module docstring.
    print(message, flush=True)


class ControlTarget(Protocol):
    """What the server needs from the daemon. Kept tiny so tests can pass a fake."""

    protocol_version: int

    def dispatch(self, name: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    def snapshot(self) -> dict[str, Any]: ...
    def add_snapshot_listener(self, listener) -> None: ...


def socket_path() -> Path:
    """
    $XDG_RUNTIME_DIR/lunos/control.sock, falling back to ~/.cache/lunos/ when there
    is no runtime dir (a bare ssh session).

    The subdirectory is not tidiness: `--filesystem=xdg-run/lunos` can share a
    directory into a Flatpak sandbox, while a bare $XDG_RUNTIME_DIR/lunos.sock
    could not be shared at all.
    """
    override = os.environ.get(SOCKET_PATH_ENV_VAR)
    if override:
        return Path(override).expanduser()
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime_dir) if runtime_dir else Path.home() / ".cache"
    return base / SOCKET_DIRECTORY_NAME / SOCKET_FILE_NAME


def _is_live_socket(path: Path) -> bool:
    """True if something is actually listening - as opposed to a stale socket file
    left behind by a crash (or by a run without $XDG_RUNTIME_DIR's auto-cleanup)."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


class _Handler(socketserver.StreamRequestHandler):
    """One connection: request/reply lines, or a push stream after `subscribe`."""

    def handle(self) -> None:
        server: ControlServer = self.server  # type: ignore[assignment]
        # A banner on connect makes a hand-driven socat session self-describing and
        # lets a client check the protocol version before it sends anything.
        self._send({
            "ok": True,
            "banner": "lunos control socket",
            "commands": sorted(COMMANDS),
        })

        while True:
            # readline(limit) stops at the limit instead of buffering forever; a line
            # that hits it without a terminator is the oversized case.
            line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if not line:
                return
            if len(line) > MAX_REQUEST_BYTES:
                self._send({"ok": False, "error": "request too large"})
                return

            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue

            try:
                request = json.loads(text)
            except json.JSONDecodeError as error:
                self._send({"ok": False, "error": f"malformed JSON: {error}"})
                continue

            if not isinstance(request, dict):
                self._send({"ok": False, "error": "expected a JSON object"})
                continue

            command = request.get("cmd")
            if not isinstance(command, str):
                self._send({"ok": False, "error": "missing \"cmd\""})
                continue

            if command == "subscribe":
                self._send({"ok": True, "subscribed": True, "state": server.app.snapshot()})
                self._push_snapshots(server)
                return

            if command not in COMMANDS:
                self._send({"ok": False, "error": f"unknown command: {command}"})
                continue

            # The request object doubles as the payload, so {"cmd": "set_offset",
            # "offset_pct": 10} works as well as a nested payload would.
            self._send(server.app.dispatch(command, request))

    def _push_snapshots(self, server: "ControlServer") -> None:
        """After `subscribe` the connection is push-only: the client stops being read
        and gets one state object per daemon loop iteration until it goes away."""
        updates: queue.Queue = queue.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        server.add_subscriber(updates)
        try:
            while True:
                state = updates.get()
                if state is _SHUTDOWN:
                    return
                self._send({"ok": True, "state": state})
        except OSError:
            return  # client hung up mid-write
        finally:
            server.remove_subscriber(updates)

    def _send(self, reply: dict[str, Any]) -> None:
        reply.setdefault("protocol", self.server.app.protocol_version)  # type: ignore[attr-defined]
        self.wfile.write((json.dumps(reply) + "\n").encode("utf-8"))
        self.wfile.flush()


COMMANDS = {
    "get_state",
    "get_schema",
    "set_config",
    "set_offset",
    "pause",
    "resume",
    "reload_config",
    "restart",
    "subscribe",
}


class ControlServer(socketserver.ThreadingUnixStreamServer):
    """Threaded so one blocked/subscribed client can't stall the others. Every thread
    is a daemon thread: the process exits on `restart` without waiting for clients."""

    daemon_threads = True
    request_queue_size = 8

    def __init__(self, path: Path, app: ControlTarget):
        self.path = path
        self.app = app
        self._subscribers: set[queue.Queue] = set()
        self._subscribers_lock = threading.Lock()

        self._prepare_path(path)
        super().__init__(str(path), _Handler)
        os.chmod(path, 0o600)
        app.add_snapshot_listener(self.broadcast)

    @staticmethod
    def _prepare_path(path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)  # a pre-existing directory may be more permissive
        if path.exists():
            if _is_live_socket(path):
                raise OSError(f"another Lunos daemon is already listening on {path}")
            path.unlink()

    def add_subscriber(self, updates: queue.Queue) -> None:
        with self._subscribers_lock:
            self._subscribers.add(updates)

    def remove_subscriber(self, updates: queue.Queue) -> None:
        with self._subscribers_lock:
            self._subscribers.discard(updates)

    def broadcast(self, state: dict[str, Any]) -> None:
        """Snapshot listener - runs on the daemon's loop thread, so it must never block."""
        with self._subscribers_lock:
            subscribers = list(self._subscribers)
        for updates in subscribers:
            try:
                updates.put_nowait(state)
            except queue.Full:
                # Drop the oldest, keep the newest: a slow client should see current
                # state when it catches up, not a queue of history.
                try:
                    updates.get_nowait()
                    updates.put_nowait(state)
                except (queue.Empty, queue.Full):
                    pass

    def shutdown(self) -> None:
        with self._subscribers_lock:
            subscribers = list(self._subscribers)
        for updates in subscribers:
            try:
                updates.put_nowait(_SHUTDOWN)
            except queue.Full:
                pass
        super().shutdown()

    def server_close(self) -> None:
        super().server_close()
        try:
            self.path.unlink()
        except OSError:
            pass


def serve(app: ControlTarget, path: Path | None = None, poll_interval: float = 0.5) -> ControlServer | None:
    """
    Starts the control server in a background thread. Returns None (and logs) when
    the socket cannot be created: brightness control is the daemon's job and must
    not depend on the GUI's IPC surface being available.

    poll_interval is how often serve_forever() checks for shutdown; the default
    keeps idle wakeups rare, and tests shorten it so teardown isn't the slowest
    thing in the suite.
    """
    target = path or socket_path()
    try:
        server = ControlServer(target, app)
    except OSError as error:
        log(f"Control socket unavailable ({error}); continuing without it")
        return None

    threading.Thread(
        target=server.serve_forever, args=(poll_interval,), name="lunos-control", daemon=True
    ).start()
    log(f"Control socket listening on {target}")
    return server
