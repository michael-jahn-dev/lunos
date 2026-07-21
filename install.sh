#!/usr/bin/env bash
set -e

# Determines the directory this script itself lives in - regardless of where it's invoked from
PROJECT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
VENV_DIR="$PROJECT_DIR/venv"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/lunos.service"

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
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$VENV_DIR/bin/python3 $PROJECT_DIR/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now lunos.service

echo ""
echo "Lunos is running. Check status with:"
echo "  systemctl --user status lunos.service"
echo "Watch logs live with:"
echo "  journalctl --user -u lunos.service -f"