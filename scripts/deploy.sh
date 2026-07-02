#!/usr/bin/env bash
# Deploy exaviz-poe-mqtt-bridge to the Cruiser host over SSH.
#
# Usage: scripts/deploy.sh user@host [config.yaml]
#
# Copies the project, installs it in a venv at /opt/exaviz-poe-mqtt-bridge,
# installs the config (if given) and the systemd unit, but does NOT enable
# the service — first runs are expected in the foreground:
#
#   ssh user@host sudo /opt/exaviz-poe-mqtt-bridge/bin/exaviz-poe-mqtt-bridge \
#       --config /etc/exaviz-poe-mqtt-bridge/config.yaml --log-level debug
set -euo pipefail

TARGET="${1:?usage: deploy.sh user@host [config.yaml]}"
CONFIG="${2:-}"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_SRC="/tmp/exaviz-poe-mqtt-bridge-src"

echo "==> Copying sources to $TARGET:$REMOTE_SRC"
rsync -az --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude '*.egg-info' --exclude 'config.local.yaml' \
  --exclude 'build' --exclude '.claude' \
  "$SRC_DIR/" "$TARGET:$REMOTE_SRC/"

if [[ -n "$CONFIG" ]]; then
  echo "==> Copying config"
  scp "$CONFIG" "$TARGET:/tmp/exaviz-poe-bridge-config.yaml"
fi

echo "==> Installing on $TARGET"
ssh "$TARGET" 'sudo bash -s' <<'REMOTE'
set -euo pipefail

# venv keeps the system Python untouched
python3 -m venv /opt/exaviz-poe-mqtt-bridge 2>/dev/null || true
/opt/exaviz-poe-mqtt-bridge/bin/pip install -q --upgrade pip
/opt/exaviz-poe-mqtt-bridge/bin/pip install -q /tmp/exaviz-poe-mqtt-bridge-src

mkdir -p /etc/exaviz-poe-mqtt-bridge
if [[ -f /tmp/exaviz-poe-bridge-config.yaml ]]; then
  mv /tmp/exaviz-poe-bridge-config.yaml /etc/exaviz-poe-mqtt-bridge/config.yaml
  chmod 600 /etc/exaviz-poe-mqtt-bridge/config.yaml
fi

# systemd unit pointing at the venv binary
sed 's|/usr/local/bin/exaviz-poe-mqtt-bridge|/opt/exaviz-poe-mqtt-bridge/bin/exaviz-poe-mqtt-bridge|' \
  /tmp/exaviz-poe-mqtt-bridge-src/systemd/exaviz-poe-mqtt-bridge.service \
  > /etc/systemd/system/exaviz-poe-mqtt-bridge.service
systemctl daemon-reload

echo "--- sanity checks ---"
ls -la /dev/pse 2>/dev/null || echo "WARNING: /dev/pse not found (exaviz-dkms installed?)"
ls /sys/class/net/ | grep -c '^poe' | xargs -I{} echo "poe interfaces: {}"
/opt/exaviz-poe-mqtt-bridge/bin/exaviz-poe-mqtt-bridge --version
echo "Install OK. Foreground test:"
echo "  sudo /opt/exaviz-poe-mqtt-bridge/bin/exaviz-poe-mqtt-bridge --config /etc/exaviz-poe-mqtt-bridge/config.yaml --log-level debug"
echo "Then enable permanently with:"
echo "  sudo systemctl enable --now exaviz-poe-mqtt-bridge"
REMOTE

echo "==> Done"
