#!/usr/bin/env python3
"""
Lunos tray app - a thin GUI client for the Lunos daemon.

Runs as its own process and talks to the daemon over its control socket
($XDG_RUNTIME_DIR/lunos/control.sock). It imports nothing from main.py: the
config schema, the current values and the live state all arrive over the wire,
which is what lets this run on the system interpreter with the distro's PySide6
(`sudo dnf install python3-pyside6`) while the daemon keeps running from its own
venv.

Quitting or crashing this app does not stop auto-brightness - the daemon is a
separate systemd user service and keeps going.

    python3 tray.py
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QPainter, QPalette, QPen
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSystemTrayIcon,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# Must match the .desktop file installed to ~/.local/share/applications/. Under
# Wayland the compositor derives the window's icon and task-manager identity from
# the app_id, which Qt takes from setDesktopFileName() - not from setWindowIcon().
APP_ID = "dev.michaeljahn.Lunos"

PROTOCOL_VERSION = 1
RECONNECT_INTERVAL_MS = 2000
OFFSET_MENU_RANGE = 30   # the tray submenu covers +/- this...
OFFSET_MENU_STEP = 5     # ...in these steps. The settings window has the full slider.

SECTION_TITLES = {
    "sensor": "Sensor",
    "curve": "Curve",
    "behaviour": "Behaviour",
    "backend": "Backend",
    "notifications": "Notifications",
}

# Verified against Breeze with QIcon.hasThemeIcon(): every first choice here exists.
# The later entries are for other icon themes (Adwaita and friends), not decoration.
ICON_NAMES = {
    "normal": ("video-display-brightness-symbolic", "high-brightness-symbolic", "display-brightness-symbolic"),
    "paused": ("low-brightness-symbolic", "brightness-low-symbolic", "display-brightness-low-symbolic"),
    "error": ("dialog-warning", "state-warning", "dialog-error"),
}

BUNDLED_ICON = Path(__file__).resolve().parent / "assets" / "lunos.svg"


def control_socket_path() -> Path:
    """Same resolution order as control.py's socket_path()."""
    override = os.environ.get("LUNOS_CONTROL_SOCKET")
    if override:
        return Path(override).expanduser()
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime_dir) if runtime_dir else Path.home() / ".cache"
    return base / "lunos" / "control.sock"


def single_instance_socket_path() -> Path:
    """Where a second launch asks the running instance to raise its window."""
    return control_socket_path().parent / "tray.sock"


def single_instance_lock_path() -> Path:
    """The flock target. Separate from the socket above on purpose - see SingleInstance."""
    return control_socket_path().parent / "tray.lock"


def themed_icon(status: str) -> QIcon:
    """
    Icon by *name* where possible: SNI and DBusMenu both hand the host an icon name
    when there is one, which is what makes the icon follow the user's icon theme and
    light/dark switch. A named icon that the theme lacks is serialized as a pixmap
    instead, freezing at whatever the theme was at startup.

    The existence test is hasThemeIcon(), not isNull(): in Qt 6 fromTheme() returns a
    non-null icon for a name the theme has never heard of, so an isNull() chain always
    stops at its first entry and silently ships a blank icon.
    """
    for name in ICON_NAMES[status]:
        if QIcon.hasThemeIcon(name):
            return QIcon.fromTheme(name)
    return QIcon(str(BUNDLED_ICON))


# --------------------------------------------------------------------------- #
# Control-socket client
# --------------------------------------------------------------------------- #

class DaemonClient(QObject):
    """
    Two connections to the control socket, because `subscribe` turns a connection
    into a one-way push stream: one stays request/reply for commands, the other
    subscribes and only ever receives state.

    QLocalSocket is used rather than a thread with the stdlib socket module - it
    lives on Qt's event loop, so there is no cross-thread signalling to get wrong.
    """

    stateChanged = Signal(dict)
    availabilityChanged = Signal(bool)

    def __init__(self, path: Path, parent: QObject | None = None):
        super().__init__(parent)
        self._path = str(path)
        self._available = False
        self._pending: list = []          # reply callbacks, in send order
        self._command_buffer = bytearray()
        self._event_buffer = bytearray()

        self._commands = QLocalSocket(self)
        self._commands.readyRead.connect(self._read_commands)
        self._commands.connected.connect(self._on_connected)
        self._commands.disconnected.connect(self._on_disconnected)
        self._commands.errorOccurred.connect(lambda _: self._on_disconnected())

        self._events = QLocalSocket(self)
        self._events.readyRead.connect(self._read_events)
        self._events.connected.connect(lambda: self._write(self._events, {"cmd": "subscribe"}))

        self._retry = QTimer(self)
        self._retry.setInterval(RECONNECT_INTERVAL_MS)
        self._retry.timeout.connect(self._connect_sockets)

    @property
    def available(self) -> bool:
        return self._available

    def start(self) -> None:
        self._connect_sockets()
        self._retry.start()

    def send(self, request: dict, callback=None) -> None:
        """Queues a command. Replies arrive in order, so the callback queue is a list."""
        if self._commands.state() != QLocalSocket.LocalSocketState.ConnectedState:
            if callback:
                callback({"ok": False, "error": "daemon not running"})
            return
        self._pending.append(callback)
        self._write(self._commands, request)

    # -- internals ---------------------------------------------------------- #

    def _connect_sockets(self) -> None:
        for socket in (self._commands, self._events):
            if socket.state() == QLocalSocket.LocalSocketState.UnconnectedState:
                socket.connectToServer(self._path)

    @staticmethod
    def _write(socket: QLocalSocket, request: dict) -> None:
        socket.write((json.dumps(request) + "\n").encode("utf-8"))
        socket.flush()

    def _on_connected(self) -> None:
        if not self._available:
            self._available = True
            self.availabilityChanged.emit(True)

    def _on_disconnected(self) -> None:
        for callback in self._pending:
            if callback:
                callback({"ok": False, "error": "daemon not running"})
        self._pending.clear()
        self._command_buffer.clear()
        if self._available:
            self._available = False
            self.availabilityChanged.emit(False)
        # The retry timer keeps running, so the tray recovers by itself once the
        # daemon (or its socket) comes back - "daemon not running" is a state to
        # display, not a reason to give up.

    def _read_commands(self) -> None:
        for message in self._take_messages(self._commands, self._command_buffer):
            if "banner" in message:
                if message.get("protocol") != PROTOCOL_VERSION:
                    print(
                        f"Lunos: daemon speaks protocol {message.get('protocol')}, "
                        f"this tray app speaks {PROTOCOL_VERSION}; update both halves.",
                        file=sys.stderr,
                    )
                continue
            callback = self._pending.pop(0) if self._pending else None
            if callback:
                callback(message)

    def _read_events(self) -> None:
        for message in self._take_messages(self._events, self._event_buffer):
            state = message.get("state")
            if state:
                self.stateChanged.emit(state)

    @staticmethod
    def _take_messages(socket: QLocalSocket, buffer: bytearray):
        buffer.extend(bytes(socket.readAll()))
        messages = []
        while b"\n" in buffer:
            line, _, rest = bytes(buffer).partition(b"\n")
            buffer[:] = rest
            if not line.strip():
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return messages


# --------------------------------------------------------------------------- #
# Curve preview + bucket editing
# --------------------------------------------------------------------------- #

def bucket_problems(rows: list[list[float]]) -> tuple[list[str], list[str]]:
    """
    Validates a bucket table the way the daemon does, plus the one rule that only
    matters visually: adjacent buckets must OVERLAP.

    The overlap is the hysteresis - the whole reason the curve is a bucket table
    rather than a formula. A table without it flickers between two brightness
    levels whenever the light sits near a boundary, so a missing overlap is the
    warning and a present one is correct. Getting this backwards would be the most
    damaging possible bug in this dialog.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if not rows:
        return ["The curve needs at least one bucket."], warnings

    for index, (min_lux, max_lux, brightness) in enumerate(rows, start=1):
        if min_lux < 0 or max_lux < 0:
            errors.append(f"Bucket {index}: lux values cannot be negative.")
        if min_lux >= max_lux:
            errors.append(f"Bucket {index}: min lux must be below max lux.")
        if not 1 <= brightness <= 100:
            errors.append(f"Bucket {index}: brightness must be between 1 and 100%.")

    for index, (previous, current) in enumerate(zip(rows, rows[1:]), start=1):
        if current[2] <= previous[2]:
            errors.append(f"Bucket {index + 1}: brightness must be higher than bucket {index}.")
        if current[0] >= previous[1]:
            warnings.append(
                f"Buckets {index} and {index + 1} do not overlap: brightness will flicker "
                f"when the light sits near {previous[1]:g} lx. The overlap is the hysteresis."
            )
    return errors, warnings


class CurvePreview(QWidget):
    """Draws each bucket as a bar spanning its lux range at its brightness, so the
    overlap between neighbours is visible rather than something to reason about."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._rows: list[list[float]] = []
        self.setMinimumHeight(140)

    def set_rows(self, rows: list[list[float]]) -> None:
        self._rows = rows
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(8, 8, -8, -8)

        painter.setPen(QPen(self.palette().mid().color(), 1))
        painter.drawRect(rect)
        if not self._rows:
            return

        max_lux = max(row[1] for row in self._rows) or 1.0
        accent = self.palette().highlight().color()
        for index, (min_lux, max_lux_row, brightness) in enumerate(self._rows):
            x0 = rect.left() + rect.width() * (min_lux / max_lux)
            x1 = rect.left() + rect.width() * (max_lux_row / max_lux)
            y = rect.bottom() - rect.height() * (brightness / 100.0)
            colour = QColor(accent)
            colour.setAlpha(90 + (index * 20) % 120)  # neighbours stay distinguishable where they overlap
            painter.setPen(QPen(colour, 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(int(x0), int(y), int(x1), int(y))

        painter.setPen(self.palette().text().color())
        painter.drawText(rect.adjusted(2, 2, -2, -2), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, "100%")
        painter.drawText(
            rect.adjusted(2, 2, -2, -2),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
            f"{max_lux:g} lx",
        )


class BucketTable(QWidget):
    """The bucket editor: a table plus add/remove/reset and the live problem list."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._default_rows: list[list[float]] = []

        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["Min lux", "Max lux", "Brightness %"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.itemChanged.connect(lambda _: self._refresh())

        self.preview = CurvePreview(self)
        self.problems = QLabel(self)
        self.problems.setWordWrap(True)

        add = QPushButton("Add bucket", self)
        remove = QPushButton("Remove selected", self)
        reset = QPushButton("Reset to defaults", self)
        add.clicked.connect(self._add_row)
        remove.clicked.connect(self._remove_selected)
        reset.clicked.connect(lambda: self.set_rows(self._default_rows))

        buttons = QHBoxLayout()
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addWidget(reset)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addLayout(buttons)
        layout.addWidget(self.preview)
        layout.addWidget(self.problems)

    def set_default_rows(self, rows: list[list[float]]) -> None:
        self._default_rows = [list(row) for row in rows]

    def set_rows(self, rows: list[list[float]]) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column, value in enumerate(row):
                self.table.setItem(row_index, column, QTableWidgetItem(f"{value:g}"))
        self.table.blockSignals(False)
        self._refresh()

    def rows(self) -> list[list[float]]:
        rows = []
        for row_index in range(self.table.rowCount()):
            values = []
            for column in range(3):
                item = self.table.item(row_index, column)
                try:
                    values.append(float(item.text()) if item else 0.0)
                except ValueError:
                    values.append(float("nan"))
            rows.append(values)
        return rows

    def serialized_rows(self) -> list[list[float]]:
        return [[row[0], row[1], int(row[2])] for row in self.rows()]

    def errors(self) -> list[str]:
        return bucket_problems(self.rows())[0]

    def _add_row(self) -> None:
        rows = self.rows()
        last = rows[-1] if rows else [0, 10, 5]
        rows.append([last[1] * 0.5, last[1] * 2, min(100, int(last[2]) + 15)])
        self.set_rows(rows)

    def _remove_selected(self) -> None:
        for index in sorted((i.row() for i in self.table.selectedIndexes()), reverse=True):
            self.table.removeRow(index)
        self._refresh()

    def _refresh(self) -> None:
        rows = self.rows()
        self.preview.set_rows(rows)
        errors, warnings = bucket_problems(rows)
        messages = [f"⛔ {message}" for message in errors] + [f"⚠ {message}" for message in warnings]
        self.problems.setText("\n".join(messages) if messages else "✓ Curve looks fine.")


# --------------------------------------------------------------------------- #
# Settings window
# --------------------------------------------------------------------------- #

class SettingsDialog(QDialog):
    """
    Built entirely from the daemon's `get_schema` reply, so a new Config field
    needs no code here beyond its entry in the daemon's FIELD_SPECS table.

    Client-side validation is a convenience; the daemon validates again and its
    verdict is the one that counts (it is what writes the file and drives the
    monitor).
    """

    def __init__(self, client: DaemonClient, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Lunos Settings")
        self.resize(640, 620)
        self._client = client
        self._editors: dict[str, QWidget] = {}
        self._schema: dict[str, dict] = {}
        self._state: dict = {}

        self.tabs = QTabWidget(self)
        self.status = QLabel(self)
        self.status.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Close
            | QDialogButtonBox.StandardButton.RestoreDefaults,
            self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.apply_changes)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self._restore_defaults)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addWidget(self.status)
        layout.addWidget(buttons)

        client.stateChanged.connect(self._on_state)

    # -- building ----------------------------------------------------------- #

    def load(self) -> None:
        self._client.send({"cmd": "get_schema"}, self._build)
        self._client.send({"cmd": "get_state"}, lambda reply: self._on_state(reply.get("state", {})))

    def _build(self, reply: dict) -> None:
        if not reply.get("ok"):
            self.status.setText(f"Could not read settings: {reply.get('error', 'daemon not running')}")
            return

        self.tabs.clear()
        self._editors.clear()
        self._schema = {entry["name"]: entry for entry in reply["schema"]}

        pages: dict[str, QFormLayout] = {}
        for section, title in SECTION_TITLES.items():
            page = QWidget(self)
            form = QFormLayout(page)
            pages[section] = form
            # Explanations make the longer tabs taller than any sensible default window,
            # so each tab scrolls rather than putting fields out of reach.
            scroller = QScrollArea(self)
            scroller.setWidget(page)
            scroller.setWidgetResizable(True)
            scroller.setFrameShape(QScrollArea.Shape.NoFrame)
            self.tabs.addTab(scroller, title)

        for name, entry in self._schema.items():
            form = pages.get(entry["section"])
            if form is None:
                continue
            editor = self._make_editor(entry)
            if editor is None:
                continue
            self._editors[name] = editor
            label = QLabel(entry["label"] + (f" ({entry['unit']})" if entry["unit"] else ""))
            label.setWordWrap(True)
            if entry["apply"] == "restart":
                label.setText(label.text() + "  ·  applies at next start")
            if entry["help"]:
                label.setToolTip(entry["help"])
                editor.setToolTip(entry["help"])
            # The explanation goes *under* the field, not only in a tooltip: these
            # settings are the reason the app exists, and a tooltip is invisible to
            # anyone who doesn't already suspect there is something to hover over.
            form.addRow(label, self._with_explanation(editor, entry["help"]))

        self._add_extras(pages)

    def _explanation_label(self, text: str, parent: QWidget | None = None) -> QLabel:
        """A wrapped, slightly smaller, dimmed line of prose. Dimmed through the
        palette rather than a hardcoded grey, so it stays legible in both the light
        and dark Breeze variants."""
        label = QLabel(text, parent or self)
        label.setWordWrap(True)
        font = label.font()
        font.setPointSizeF(max(7.0, font.pointSizeF() * 0.9))
        label.setFont(font)
        palette = label.palette()
        palette.setColor(
            QPalette.ColorRole.WindowText,
            palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText),
        )
        label.setPalette(palette)
        return label

    def _with_explanation(self, editor: QWidget, help_text: str) -> QWidget:
        """Stacks the editor above its explanation. Returns the editor unchanged when
        there is nothing to say, so `self._editors[name]` always points at the real
        widget rather than this wrapper."""
        if not help_text:
            return editor

        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(2)
        layout.addWidget(editor)
        layout.addWidget(self._explanation_label(help_text, container))
        return container

    def _make_editor(self, entry: dict) -> QWidget | None:
        kind, value = entry["kind"], entry["current"]

        if kind == "bool":
            editor = QCheckBox(self)
            editor.setChecked(bool(value))
            return editor

        if kind in ("int", "float"):
            # A float field's bounds must stay fractional (0.5 s would floor to 0 and
            # then reject the very default it came with).
            cast = int if kind == "int" else float
            editor = QSpinBox(self) if kind == "int" else QDoubleSpinBox(self)
            if kind == "float":
                editor.setDecimals(2)
            editor.setRange(
                cast(entry["min"]) if entry["min"] is not None else cast(-1_000_000),
                cast(entry["max"]) if entry["max"] is not None else cast(1_000_000),
            )
            if entry["step"]:
                editor.setSingleStep(cast(entry["step"]) if kind == "float" else max(1, int(entry["step"])))
            editor.setValue(cast(value))
            return editor

        if kind == "buckets":
            table = BucketTable(self)
            table.set_default_rows(entry["default"])
            table.set_rows(entry["current"])
            return table

        editor = QLineEdit(self)
        editor.setText("" if value is None else str(value))
        if entry["optional"]:
            editor.setPlaceholderText("(unset)")
        return editor

    def _add_extras(self, pages: dict[str, QFormLayout]) -> None:
        """The parts a generic schema cannot express: live status readouts, the
        offset slider, and the sensor test button."""
        self.sensor_status = QLabel(self)
        self.sensor_status.setWordWrap(True)
        test = QPushButton("Test connection", self)
        test.clicked.connect(
            lambda: self._client.send({"cmd": "get_state"}, lambda reply: self._on_state(reply.get("state", {})))
        )
        pages["sensor"].addRow(test, self.sensor_status)

        self.offset_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.offset_slider.setRange(-50, 50)
        self.offset_slider.setTickInterval(10)
        self.offset_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.offset_value = QLabel("+0%", self)
        self.offset_slider.valueChanged.connect(lambda value: self.offset_value.setText(f"{value:+d}%"))
        self.offset_slider.sliderReleased.connect(
            lambda: self._client.send({"cmd": "set_offset", "offset_pct": self.offset_slider.value()})
        )
        offset_row = QWidget(self)
        offset_layout = QHBoxLayout(offset_row)
        offset_layout.setContentsMargins(0, 0, 0, 0)
        offset_layout.addWidget(self.offset_slider)
        offset_layout.addWidget(self.offset_value)
        pages["behaviour"].insertRow(0, "Brightness offset", offset_row)
        pages["behaviour"].insertRow(1, self._explanation_label(
            "Added to every bucket target before it is applied, so the whole curve sits brighter or "
            "darker than the table says. Changing it here takes effect at once and ends any pause "
            "left over from a manual change."
        ))

        self.backend_status = QLabel(self)
        self.backend_status.setWordWrap(True)
        pages["backend"].addRow("Status", self.backend_status)


    # -- live state --------------------------------------------------------- #

    def _on_state(self, state: dict) -> None:
        if not state:
            return
        self._state = state
        if hasattr(self, "offset_slider") and not self.offset_slider.isSliderDown():
            self.offset_slider.blockSignals(True)
            self.offset_slider.setValue(state.get("offset_pct", 0))
            self.offset_value.setText(f"{state.get('offset_pct', 0):+d}%")
            self.offset_slider.blockSignals(False)

        if hasattr(self, "sensor_status"):
            if state.get("sensor_connected"):
                self.sensor_status.setText(f"Connected · {state.get('raw_lux', 0):.0f} lx")
            else:
                self.sensor_status.setText(f"Not connected · {state.get('last_error') or 'waiting'}")

        if hasattr(self, "backend_status"):
            backend = "KDE PowerDevil" if state.get("backend") == "powerdevil" else "ddcutil (direct DDC/CI)"
            pending = " · still watching for PowerDevil" if state.get("powerdevil_pending") else ""
            self.backend_status.setText(f"{backend}{pending} · brightness {state.get('brightness_pct', 0)}%")

    # -- applying ----------------------------------------------------------- #

    def _collect(self) -> tuple[dict, list[str]]:
        fields: dict = {}
        errors: list[str] = []
        for name, editor in self._editors.items():
            entry = self._schema[name]
            if isinstance(editor, QCheckBox):
                value = editor.isChecked()
            elif isinstance(editor, (QSpinBox, QDoubleSpinBox)):
                value = editor.value()
            elif isinstance(editor, BucketTable):
                errors.extend(editor.errors())
                value = editor.serialized_rows()
            else:
                text = editor.text().strip()
                value = None if (not text and entry["optional"]) else text
            if value != entry["current"]:
                fields[name] = value
        return fields, errors

    def apply_changes(self) -> None:
        fields, errors = self._collect()
        if errors:
            self.status.setText("\n".join(errors))
            return
        if not fields:
            self.status.setText("No changes.")
            return
        self._client.send({"cmd": "set_config", "fields": fields}, self._on_applied)

    def _on_applied(self, reply: dict) -> None:
        if not reply.get("ok"):
            details = reply.get("errors") or {"": reply.get("error", "unknown error")}
            self.status.setText("\n".join(f"{name}: {message}" for name, message in details.items()))
            return

        notes = [f"Applied: {', '.join(reply['applied'])}"] if reply.get("applied") else []
        if reply.get("reconnecting"):
            notes.append(f"Reconnecting to the sensor for: {', '.join(reply['reconnecting'])}")
        if reply.get("restart_required"):
            notes.append(f"Takes effect at the next start: {', '.join(reply['restart_required'])}")
        self.status.setText(" · ".join(notes) or "Saved.")
        self._client.send({"cmd": "get_schema"}, self._build)  # re-read so 'current' matches reality

    def _restore_defaults(self) -> None:
        for name, editor in self._editors.items():
            default = self._schema[name]["default"]
            if isinstance(editor, QCheckBox):
                editor.setChecked(bool(default))
            elif isinstance(editor, (QSpinBox, QDoubleSpinBox)):
                editor.setValue(default)
            elif isinstance(editor, BucketTable):
                editor.set_rows(default)
            else:
                editor.setText("" if default is None else str(default))
        self.status.setText("Defaults loaded - press Apply to save them.")


# --------------------------------------------------------------------------- #
# Tray icon
# --------------------------------------------------------------------------- #

class LunosTray(QObject):
    """
    The SNI item and its menu.

    Everything here has to survive DBusMenu, which serializes menu *items* only:
    labels, checkboxes, radio groups, submenus, separators. QWidgetAction (the
    obvious way to put a slider in the menu) renders in a local Qt popup and is
    silently dropped by the real tray - hence the offset radio submenu, with the
    actual slider living in the settings window.
    """

    def __init__(self, client: DaemonClient, app: QApplication):
        super().__init__(app)
        self._client = client
        self._app = app
        self._state: dict = {}
        self._settings: SettingsDialog | None = None

        self.icon = QSystemTrayIcon(themed_icon("normal"), self)
        self.menu = QMenu()

        # Heading plus one state line, as two disabled items rather than one item
        # holding a newline: DBusMenu carries a menu *item's* label, and a host is
        # free to render an embedded "\n" as a space or to clip at it. Separate
        # items break where they are meant to, everywhere.
        # One disabled line carrying the whole summary. A separate styled heading was
        # tried and dropped: DBusMenu carries no font, size or markup property, so a
        # heading item can only ever be another plain line. (Plasma's own applets -
        # clipboard, battery, brightness - are QML inside plasmashell, not menus,
        # which is why they can style text at all.)
        self._state_action = self.menu.addAction("Lunos - connecting…")
        self._state_action.setEnabled(False)
        self.menu.addSeparator()

        self._offset_menu = self.menu.addMenu("Brightness offset")
        self._offset_group = QActionGroup(self)
        self._offset_group.setExclusive(True)
        self._offset_actions: dict[int, QAction] = {}
        for offset in range(-OFFSET_MENU_RANGE, OFFSET_MENU_RANGE + 1, OFFSET_MENU_STEP):
            action = self._offset_menu.addAction(f"{offset:+d}%")
            action.setCheckable(True)
            self._offset_group.addAction(action)
            action.triggered.connect(lambda _checked=False, value=offset: self._set_offset(value))
            self._offset_actions[offset] = action
        self._offset_menu.addSeparator()
        self._offset_menu.addAction("Reset to 0%", lambda: self._set_offset(0))

        self.menu.addSeparator()
        self._pause_action = self.menu.addAction("Pause auto-adjustment")
        self._pause_action.setCheckable(True)
        self._pause_action.triggered.connect(self._toggle_pause)

        self.menu.addSeparator()
        self.menu.addAction("Settings…", self.open_settings)
        self._restart_action = self.menu.addAction("Restart daemon", self._restart_daemon)
        self._start_action = self.menu.addAction("Start daemon", self._start_daemon)
        self._start_action.setVisible(False)

        self.menu.addSeparator()
        # Deliberately explicit: quitting the applet is not "turn Lunos off".
        self.menu.addAction("Quit tray app (daemon keeps running)", app.quit)

        # Plasma re-reads the menu when it opens; refreshing here keeps a stale
        # header from being the first thing the user sees.
        self.menu.aboutToShow.connect(self._refresh_menu)

        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._on_activated)

        client.stateChanged.connect(self._on_state)
        client.availabilityChanged.connect(self._on_availability)

    def show(self) -> None:
        self.icon.show()

    # -- state -------------------------------------------------------------- #

    def _on_state(self, state: dict) -> None:
        self._state = state
        self._refresh_menu()

    def _on_availability(self, available: bool) -> None:
        self._restart_action.setVisible(available)
        self._start_action.setVisible(not available)
        if not available:
            self._state = {}
        self._refresh_menu()

    def _refresh_menu(self) -> None:
        state = self._state
        if not self._client.available:
            self._state_action.setText("Lunos - daemon not running")
            self.icon.setToolTip("Lunos: daemon not running")
            self.icon.setIcon(themed_icon("error"))
            return

        lux = state.get("median_lux")
        brightness = state.get("brightness_pct", 0)
        bucket = state.get("bucket_index", 0) + 1
        bucket_pct = state.get("bucket_pct", 0)
        offset = state.get("offset_pct", 0)
        lux_text = f"{lux:.0f} lx" if lux is not None else "no reading"

        # Both percentages earn their place: the bucket's is what the curve asks for,
        # the brightness is where the monitor actually ended up - they differ by the
        # offset, and by the minimum-brightness floor.
        state_line = f"Lunos ~ {lux_text} · {brightness}% · bucket {bucket} ({bucket_pct}%) · {offset:+d}%"
        self._state_action.setText(state_line)

        paused = bool(state.get("paused"))
        override = bool(state.get("override_active"))
        self._pause_action.setChecked(paused or override)
        if override and not paused:
            self._pause_action.setText(
                f"Pause auto-adjustment (manual change, {state.get('override_seconds_left', 0):.0f}s left)"
            )
        else:
            self._pause_action.setText("Pause auto-adjustment")

        action = self._offset_actions.get(offset)
        if action is not None:
            action.setChecked(True)
        elif self._offset_group.checkedAction():
            # An offset the submenu can't represent (not a multiple of the step, or
            # beyond its range) must not leave a wrong radio checked.
            self._offset_group.checkedAction().setChecked(False)

        connection = "" if state.get("sensor_connected") else " · sensor disconnected"
        self.icon.setToolTip(f"{state_line}{connection}")

        if not state.get("sensor_connected"):
            self.icon.setIcon(themed_icon("error"))
        elif paused or override or offset:
            self.icon.setIcon(themed_icon("paused"))
        else:
            self.icon.setIcon(themed_icon("normal"))

    # -- actions ------------------------------------------------------------ #

    def _set_offset(self, offset: int) -> None:
        self._client.send({"cmd": "set_offset", "offset_pct": offset})

    def _toggle_pause(self, checked: bool) -> None:
        self._client.send({"cmd": "pause" if checked else "resume"})

    def _restart_daemon(self) -> None:
        # Over the socket, not systemctl: the unit has Restart=always, so a clean
        # exit is a restart - and this is the only form that would survive a
        # sandboxed tray with no systemctl of its own.
        self._client.send({"cmd": "restart"})

    def _start_daemon(self) -> None:
        # The one action with no socket to talk to, so it does need systemctl.
        if not shutil.which("systemctl"):
            QMessageBox.information(
                None, "Lunos",
                "systemctl was not found. Start the daemon with:\n\n"
                "    systemctl --user start lunos.service",
            )
            return
        subprocess.run(["systemctl", "--user", "start", "lunos.service"], check=False)

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.open_settings()

    def open_settings(self) -> None:
        if self._settings is None:
            self._settings = SettingsDialog(self._client)
        self._settings.load()
        self._settings.show()
        self._settings.raise_()
        self._settings.activateWindow()


# --------------------------------------------------------------------------- #
# Single instance + entry point
# --------------------------------------------------------------------------- #

class SingleInstance(QObject):
    """
    Two tray icons is a visible bug, so a second launch hands its request to the
    first instance (which raises its settings window) and exits.

    The mutual exclusion is an `flock`, not the QLocalServer bind. QLocalServer
    does *not* fail to listen on a path another instance is already serving - it
    replaces the socket file and reports success - so a lock built on "listen
    failed, therefore someone else is running" silently lets every launch through.
    flock is atomic, so two simultaneous launches can't both win, and the kernel
    drops it if the process is killed, so a crash leaves nothing stale behind.

    The socket is then only the raise-the-window channel, held by whoever owns the
    lock.
    """

    raiseRequested = Signal()

    def __init__(self, socket_path: Path, lock_path: Path, parent: QObject | None = None):
        super().__init__(parent)
        self._socket_path = str(socket_path)
        self._lock_path = str(lock_path)
        self._lock_file = None  # kept open for the process's lifetime: closing it drops the lock
        self._server: QLocalServer | None = None

    def claim(self) -> bool:
        """True if this process now owns the lock and is the only instance."""
        Path(self._lock_path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_file = open(self._lock_path, "w")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_file.close()
            self._ask_running_instance_to_raise()
            return False

        self._lock_file = lock_file
        # We hold the lock, so any socket file here is ours to replace (a crashed
        # instance leaves one behind).
        QLocalServer.removeServer(self._socket_path)
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if server.listen(self._socket_path):
            server.newConnection.connect(self._on_connection)
            self._server = server
        return True

    def _ask_running_instance_to_raise(self) -> None:
        probe = QLocalSocket()
        probe.connectToServer(self._socket_path)
        if not probe.waitForConnected(500):
            return  # lock held but nobody answering; nothing useful to do but exit
        probe.write(b"raise\n")
        probe.flush()
        probe.waitForBytesWritten(500)
        probe.disconnectFromServer()

    def _on_connection(self) -> None:
        connection = self._server.nextPendingConnection()
        connection.readyRead.connect(lambda: self.raiseRequested.emit())
        connection.disconnected.connect(connection.deleteLater)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Lunos")
    app.setDesktopFileName(APP_ID)  # mandatory on Wayland: this is the window's identity
    app.setWindowIcon(themed_icon("normal"))
    app.setQuitOnLastWindowClosed(False)  # closing the settings window must not quit the tray

    instance = SingleInstance(single_instance_socket_path(), single_instance_lock_path(), app)
    if not instance.claim():
        return 0  # the running instance was asked to raise its window

    client = DaemonClient(control_socket_path(), app)
    tray = LunosTray(client, app)
    instance.raiseRequested.connect(tray.open_settings)

    if QSystemTrayIcon.isSystemTrayAvailable():
        tray.show()
    else:
        # Silently dropping the icon is the worst outcome, and it is what happens on
        # GNOME without the AppIndicator extension: no watcher owns
        # org.kde.StatusNotifierWatcher, so the item goes nowhere.
        QMessageBox.warning(
            None, "Lunos",
            "No system tray was found.\n\n"
            "On GNOME, install the AppIndicator extension:\n"
            "    sudo dnf install gnome-shell-extension-appindicator\n"
            "and enable it, then log out and back in.\n\n"
            "The settings window is opened instead; the daemon keeps running either way.",
        )
        tray.open_settings()

    client.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
