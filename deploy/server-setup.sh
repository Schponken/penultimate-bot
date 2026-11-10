#!/bin/bash
# Sets up a fresh Ubuntu server to run Penultimate 24/7. Run as root from an unpacked copy of the repository;
# the short text you paste into the cloud provider's "Startup scripts" box does that for you.
# Reads from the environment: TAILSCALE_KEY (private access from your own devices), REPO (owner/name on GitHub),
# SHA (the version that was downloaded), GITHUB_TOKEN (only while the repository is private).
exec >> /var/log/penultimate-setup.log 2>&1
echo "== $(date -u '+%F %T') Penultimate server setup starting"
export DEBIAN_FRONTEND=noninteractive HOME=/root
SRC="$(cd "$(dirname "$0")/.." && pwd)"
step() { echo; echo "---- $*"; }

step "swap space (so the forecast model fits on a small server)"
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile \
    && echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

step "system packages"
for i in 1 2 3 4 5; do apt-get update -qq && apt-get install -y -qq python3-venv curl ufw && break; sleep 15; done

step "install and start the bot"
bash "$SRC/deploy/install-server.sh" || { echo "FAILED: bot install"; exit 1; }

step "updates from the repository"
if [ -n "${REPO:-}" ]; then
  grep -q '^update:' /opt/penultimate/config.yaml || cat >> /opt/penultimate/config.yaml << CFG

update:
  repo: $REPO
  branch: main
  auto: practice               # practice = new versions install by themselves while on fake money; with real money they wait for your click
CFG
  [ -n "${SHA:-}" ] && echo "$SHA" > /opt/penultimate/data/installed.sha
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    (umask 077; echo "$GITHUB_TOKEN" > /opt/penultimate/data/github-token)
  fi
  chown -R penultimate:penultimate /opt/penultimate
  systemctl restart penultimate
fi

step "private access from your own devices (Tailscale)"
TS_OK=0
if [ -n "${TAILSCALE_KEY:-}" ] && [ "$TAILSCALE_KEY" != "PASTE_TAILSCALE_KEY_HERE" ]; then
  curl -fsSL --retry 5 https://tailscale.com/install.sh | sh
  tailscale up --auth-key="$TAILSCALE_KEY" --hostname=penultimate && TS_OK=1
fi
if [ "$TS_OK" = 1 ]; then
  TSIP="$(tailscale ip -4 | head -1)"
  cat > /etc/systemd/system/penultimate-door.socket << UNIT
[Unit]
Description=Penultimate dashboard, reachable only over your private Tailscale network
[Socket]
ListenStream=$TSIP:80
FreeBind=true
[Install]
WantedBy=sockets.target
UNIT
  cat > /etc/systemd/system/penultimate-door.service << 'UNIT'
[Unit]
Description=Forward the private dashboard address to the bot
Requires=penultimate-door.socket
After=penultimate-door.socket
[Service]
ExecStart=/usr/lib/systemd/systemd-socket-proxyd 127.0.0.1:8787
PrivateTmp=true
UNIT
  systemctl daemon-reload && systemctl enable --now penultimate-door.socket
  # Nothing is reachable from the public internet, not even remote login. Your own devices come in over Tailscale.
  ufw default deny incoming && ufw default allow outgoing && ufw allow in on tailscale0 && ufw --force enable
  echo "Dashboard: http://penultimate/  (from a device signed in to your Tailscale)"
else
  echo "WARNING: no working Tailscale key, so the dashboard is only reachable from the server itself."
  ufw default deny incoming && ufw default allow outgoing && ufw allow OpenSSH && ufw --force enable
fi

step "check"
sleep "${CHECK_WAIT:-20}"
systemctl is-active penultimate && curl -fsS -m 10 http://127.0.0.1:8787/api/state | head -c 300
echo; echo "== $(date -u '+%F %T') Penultimate server setup finished"
