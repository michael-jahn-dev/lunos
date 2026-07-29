#!/usr/bin/env bash
set -e

# Determines the directory this script itself lives in - regardless of where it's invoked from
PROJECT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
VENV_DIR="$PROJECT_DIR/venv"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/lunos.service"
TRAY_SERVICE_FILE="$SERVICE_DIR/lunos-tray.service"
DESKTOP_DIR="$HOME/.local/share/applications"
DESKTOP_FILE="$DESKTOP_DIR/dev.michaeljahn.Lunos.desktop"

# The tray app is optional and runs on the *system* interpreter with the distro's
# PySide6, so the daemon's venv never has to carry a ~150 MB Qt dependency.
WITH_TRAY=0
for arg in "$@"; do
    case "$arg" in
        --with-tray) WITH_TRAY=1 ;;
        -h|--help)
            echo "Usage: $0 [--with-tray]"
            echo "  --with-tray   also install the system tray app (needs python3-pyside6)"
            exit 0
            ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

echo "Project directory: $PROJECT_DIR"

# Create venv and install dependencies
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

echo "Installing dependencies..."
"$VENV_DIR/bin/pip" install -q --upgrade pip
"$VENV_DIR/bin/pip" install -q -r "$PROJECT_DIR/requirements.txt"

# Create systemd service
mkdir -p "$SERVICE_DIR"

cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Lunos - Ambient Light Brightness Daemon
# plasma-powerdevil.service: start after PowerDevil where it exists (KDE), so the
# preferred backend is more likely to be up already; ignored on other desktops.
After=network-online.target plasma-powerdevil.service
Wants=network-online.target

[Service]
ExecStart=$VENV_DIR/bin/python3 $PROJECT_DIR/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

if [ "$WITH_TRAY" = "1" ]; then
    # Checked against the system interpreter, which is what the unit below runs.
    # Deliberately not auto-pip-installed: PySide6 is a large wheel and the distro
    # package is the supported path on Fedora.
    if ! /usr/bin/python3 -c "import PySide6" >/dev/null 2>&1; then
        echo "" >&2
        echo "The tray app needs PySide6 on the system interpreter. Install it with:" >&2
        echo "  sudo dnf install python3-pyside6" >&2
        echo "(non-Fedora: create a separate venv and 'pip install PySide6')" >&2
        exit 1
    fi

    mkdir -p "$DESKTOP_DIR"

    # graphical-session.target is the standard hook for session-scoped user services
    # on both Plasma 6 and GNOME. Deliberately NOT Requires=lunos.service: telling the
    # user the daemon is down is one of the tray's jobs, so it must start without it.
    cat > "$TRAY_SERVICE_FILE" << EOF
[Unit]
Description=Lunos - System Tray App
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart=/usr/bin/python3 $PROJECT_DIR/tray.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
EOF

    # Required, not cosmetic: this is what QApplication.setDesktopFileName() refers to,
    # and what gives the settings window its icon and identity on Wayland.
    cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Type=Application
Name=Lunos
GenericName=Ambient Brightness
Comment=Match monitor brightness to ambient light
Exec=/usr/bin/python3 $PROJECT_DIR/tray.py
Icon=$PROJECT_DIR/assets/lunos.svg
Terminal=false
Categories=Settings;HardwareSettings;
StartupNotify=false
SingleMainWindow=true
EOF
fi

systemctl --user daemon-reload
systemctl --user enable lunos.service
# restart (not just start) so a re-run picks up changes to main.py in an already-running service
systemctl --user restart lunos.service

if [ "$WITH_TRAY" = "1" ]; then
    systemctl --user enable lunos-tray.service
    systemctl --user restart lunos-tray.service
fi

echo ""
echo "Lunos is running. Check status with:"
echo "  systemctl --user status lunos.service"
echo "Watch logs live with:"
echo "  journalctl --user -u lunos.service -f"

if [ "$WITH_TRAY" = "1" ]; then
    echo ""
    echo "Tray app installed. Check it with:"
    echo "  systemctl --user status lunos-tray.service"
    echo "On GNOME the icon also needs: sudo dnf install gnome-shell-extension-appindicator"
fi
