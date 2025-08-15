#!/usr/bin/env bash
set -euo pipefail
SERVICE="/etc/systemd/system/hl-hostmon.service"
PREFIX="/opt/hl-hostmon"
CONFIG="/etc/hl-hostmon/config.yaml"

echo "[INFO] Stopping and disabling service (if present)"
systemctl stop hl-hostmon.service 2>/dev/null || true
systemctl disable hl-hostmon.service 2>/dev/null || true

if [[ -f "$SERVICE" ]]; then
  rm -f "$SERVICE"
  systemctl daemon-reload
  echo "[INFO] Removed unit: $SERVICE"
fi

echo "[INFO] Removing program files at $PREFIX"
rm -rf "$PREFIX"

if [[ -f "$CONFIG" ]]; then
  echo "[INFO] Keeping config at $CONFIG (remove manually if desired)."
fi

echo "[OK] Uninstall completed."
