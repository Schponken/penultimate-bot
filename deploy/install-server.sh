#!/bin/bash
# Runs Penultimate 24/7 on a small Ubuntu cloud server, whether your own computer is on or not.
# Usage on the server, from inside the unpacked penultimate-bot folder:   sudo bash deploy/install-server.sh
# It installs to /opt/penultimate, starts on boot, and restarts itself if it ever stops.
set -euo pipefail
[ "$(id -u)" = "0" ] || { echo "Run with sudo."; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
apt-get update -qq && apt-get install -y -qq python3-venv >/dev/null
id penultimate >/dev/null 2>&1 || useradd --system --home /opt/penultimate --shell /usr/sbin/nologin penultimate
mkdir -p /opt/penultimate
cp -r "$SRC"/penultimate "$SRC"/requirements.txt /opt/penultimate/
mkdir -p /opt/penultimate/deploy && cp "$SRC"/deploy/forecast-setup.sh /opt/penultimate/deploy/
[ -f /opt/penultimate/config.yaml ] || cp "$SRC"/config.yaml /opt/penultimate/
[ -f /opt/penultimate/limits.yaml ] || cp "$SRC"/limits.yaml /opt/penultimate/
python3 -m venv /opt/penultimate/.venv
/opt/penultimate/.venv/bin/pip install --quiet -r /opt/penultimate/requirements.txt
mkdir -p /opt/penultimate/data && chown -R penultimate:penultimate /opt/penultimate
cat > /etc/systemd/system/penultimate.service << 'EOF'
[Unit]
Description=Penultimate trading bot
After=network-online.target
Wants=network-online.target

[Service]
User=penultimate
WorkingDirectory=/opt/penultimate
ExecStart=/opt/penultimate/.venv/bin/python -m penultimate
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/penultimate
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable penultimate && systemctl restart penultimate
echo "Penultimate is running. The dashboard listens on this server only (127.0.0.1:8787)."
echo "View it from your own computer with:  ssh -L 8787:127.0.0.1:8787 root@THIS_SERVER  then open http://127.0.0.1:8787"
