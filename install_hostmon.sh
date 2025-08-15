#!/usr/bin/env bash
set -euo pipefail
trap 'echo "[ERROR] Failed at line $LINENO"; exit 1' ERR

VERSION="1.1.27-pre"
PREFIX="/opt/hl-hostmon"
CONFIG="/etc/hl-hostmon/config.yaml"
SERVICE="/etc/systemd/system/hl-hostmon.service"

force=0
trace=0
serial_override=""

usage() {
  cat <<EOF
ThinkLab Host - Installer v${VERSION}
Enforces exactly one connected ESP32 (strict /dev/serial/by-id path).

Usage: $0 [--force] [--trace] [--serial <path>]

Options:
  --force            Overwrite existing install/service/config/venv
  --trace            Enable JSON tracing (log_level=debug, trace_payloads=true)
  --serial <path>    Explicit serial device path; must exist (bypasses auto-detect)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) force=1; shift;;
    --trace) trace=1; shift;;
    --serial) serial_override="${2:-}"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown arg: $1"; usage; exit 1;;
  esac
done

require_root() { [[ $(id -u) -eq 0 ]] || { echo "[FATAL] Run as root."; exit 1; }; }
require_systemd() { command -v systemctl >/dev/null || { echo "[FATAL] systemd required."; exit 1; }; }

# ---- Detect exactly one ESP and return strict path
detect_one_esp() {
  local byid="/dev/serial/by-id"
  local candidates=()

  if [[ -n "$serial_override" ]]; then
    [[ -e "$serial_override" ]] || { echo "[FATAL] --serial path not found: $serial_override"; exit 1; }
    echo "$serial_override"
    return
  fi

  # Prefer Espressif by-id
  if [[ -d "$byid" ]]; then
    mapfile -t esp < <(ls -1 "$byid" 2>/dev/null | grep -i 'Espressif' || true)
    if (( ${#esp[@]} == 1 )); then
      echo "$byid/${esp[0]}"; return
    fi
    # If multiple esp by-id: fail w/ list
    if (( ${#esp[@]} > 1 )); then
      echo "[FATAL] Multiple Espressif by-id devices found:"; printf '  - %s/%s\n' "$byid" "${esp[@]}"; exit 1
    fi
    # Otherwise: any single tty-ish by-id
    mapfile -t any < <(find "$byid" -maxdepth 1 -type l -name "*tty*" -printf "%f\n" | sort || true)
    if (( ${#any[@]} == 1 )); then echo "$byid/${any[0]}"; return; fi
    if (( ${#any[@]} > 1 )); then
      echo "[FATAL] Multiple serial by-id devices present; connect only the target ESP32, or use --serial:"
      printf '  - %s/%s\n' "$byid" "${any[@]}"; exit 1
    fi
  fi

  # Fallback: raw tty’s (ACM/USB) – still enforce single device
  mapfile -t raw < <(compgen -G "/dev/ttyACM*" || true)
  mapfile -t raw_usb < <(compgen -G "/dev/ttyUSB*" || true)
  candidates=("${raw[@]}" "${raw_usb[@]}")

  # Filter out non-existent (compgen might return literal patterns)
  local filtered=()
  for p in "${candidates[@]}"; do [[ -e "$p" ]] && filtered+=("$p"); done

  if (( ${#filtered[@]} == 1 )); then echo "${filtered[0]}"; return; fi
  if (( ${#filtered[@]} == 0 )); then
    echo "[FATAL] No ESP32 serial device found. Connect exactly one ESP and try again."; exit 1
  fi
  echo "[FATAL] Multiple serial devices detected; connect only one ESP or pass --serial:"
  printf '  - %s\n' "${filtered[@]}"; exit 1
}

require_root
require_systemd

echo "[INFO] Installing OS dependencies…"
apt-get update -y >/dev/null
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3 python3-venv python3-pip smartmontools hdparm nvme-cli udev iproute2 >/dev/null

mkdir -p "$PREFIX" /etc/hl-hostmon

# Program files
cp -f "$(dirname "$0")/hostmon.py" "$PREFIX/hostmon.py"
chmod +x "$PREFIX/hostmon.py"

# Python venv
if [[ ! -d "$PREFIX/venv" || $force -eq 1 ]]; then
  echo "[INFO] Creating Python venv…"
  python3 -m venv "$PREFIX/venv"
  "$PREFIX/venv/bin/pip" install --upgrade pip >/dev/null
  "$PREFIX/venv/bin/pip" install pyserial pyyaml >/dev/null
fi

# Enforce: exactly one ESP must be connected before we write config
SERIAL_PATH="$(detect_one_esp)"
echo "[INFO] Using serial device: $SERIAL_PATH"

# Config
if [[ ! -f "$CONFIG" || $force -eq 1 ]]; then
  cat > "$CONFIG" <<YAML
serial_device: "${SERIAL_PATH}"
baud: 115200
log_level: ${trace:+debug}
trace_payloads: ${trace:+true}
YAML
  if [[ $trace -eq 0 ]]; then
    sed -i 's/log_level:.*/log_level: info/' "$CONFIG"
    sed -i 's/trace_payloads:.*/trace_payloads: false/' "$CONFIG"
  fi
  echo "[INFO] Wrote config: $CONFIG"
fi

# systemd unit
cat > "$SERVICE" <<UNIT
[Unit]
Description=Homelab Host Monitor (ESP32 serial)
After=multi-user.target
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=$PREFIX
Environment=PYTHONUNBUFFERED=1
ExecStart=$PREFIX/venv/bin/python $PREFIX/hostmon.py --config $CONFIG
Restart=always
RestartSec=2
User=root

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now hl-hostmon.service

sleep 0.5
systemctl --no-pager --full -l status hl-hostmon.service || true
echo "[OK] Installation finished."
