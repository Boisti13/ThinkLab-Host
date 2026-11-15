# ThinkLab Host (hl-hostmon)

[![Checks](https://github.com/Boisti13/ThinkLab-Host/actions/workflows/checks.yml/badge.svg)](https://github.com/Boisti13/ThinkLab-Host/actions/workflows/checks.yml)
[![Latest tag](https://img.shields.io/github/v/tag/Boisti13/ThinkLab-Host?sort=semver&label=version)](https://github.com/Boisti13/ThinkLab-Host/tags)
[![License: MIT](https://img.shields.io/github/license/Boisti13/ThinkLab-Host)](LICENSE)

A small service for a Proxmox VE host that feeds a status screen. An ESP32
on the host's USB port sends `GET`; `hl-hostmon` answers with one line of
JSON: CPU, memory, root disk, every VM and container, the disks, the IP
setup and the network rates. The screen's firmware is
[ThinkLab-ESP](https://github.com/Boisti13/ThinkLab-ESP), an e-paper
dashboard for an ESP32-C3.

No network, no open ports, no polling of its own: it only answers when the
board asks (every minute with the stock firmware).

## Highlights

- **Proxmox-aware**: every VM and container with its state, from `pvesh`
  (falls back to `qm`/`pct` when `pvesh` isn't there)
- **Never wakes a disk**: disk state comes from `hdparm -C`; no SMART reads
- **Real network rates**: totals count physical NICs only, so traffic to a
  guest isn't counted again on the bridge and its `veth`/`tap`
- **Light**: two counters kept between requests for CPU and network rates;
  about 1.6 s and 3 KB per answer on a host with 16 guests
- **Survives replugging**: reopens the serial port when the board comes back
- **Quiet**: logs only start-up and problems; a trace mode logs every
  request and answer for debugging

## Install

On the Proxmox host, as root, with the ESP32 plugged in:

```bash
git clone https://github.com/Boisti13/ThinkLab-Host.git
cd ThinkLab-Host
./install_hostmon.sh            # finds the one Espressif serial device
```

| Option | Effect |
|---|---|
| `--serial <path>` | Use this device instead of detecting it (e.g. a `/dev/serial/by-id/…` path) |
| `--trace` | Write `log_level: debug` and `trace_payloads: true` into the config |
| `--force` | Overwrite an existing config and rebuild the venv |

The installer refuses to guess: with no serial device, or more than one, it
stops and lists what it found.

| What | Where |
|---|---|
| Script | `/opt/hl-hostmon/hostmon.py` |
| Python venv (`pyserial`, `pyyaml`) | `/opt/hl-hostmon/venv` |
| Config | `/etc/hl-hostmon/config.yaml` |
| Service | `hl-hostmon.service` (root, restarts on failure) |

**Update** to a newer version: copy the new `hostmon.py` to
`/opt/hl-hostmon/` and `systemctl restart hl-hostmon` (or run the installer
again; it keeps the config). **Uninstall**: `./uninstall_hostmon.sh` (the
config stays).

## Config

```yaml
serial_device: "/dev/serial/by-id/usb-Espressif_...-if00"
baud: 115200
log_level: info        # debug for more
trace_payloads: false  # true (with log_level: debug) logs every request and answer
```

## Example

The board sends `GET` and a newline; the answer is one line (shortened):

```json
{
  "schema_version": 1,
  "script_version": "1.2.0",
  "timestamp_ms": 1794731906930,
  "hostname": "pve",
  "uptime_s": 671,
  "cpu": {"percent": 23.7, "cores": 6, "load1": 1.56, "load5": 2.06, "load15": 1.48},
  "ram": {"total_bytes": 16628801536, "used_bytes": 7731000000, "swap_total_bytes": 8589930496, "swap_used_bytes": 3006464},
  "filesystems": [{"mount": "/", "total_bytes": 100000000000, "used_bytes": 20000000000}],
  "proxmox": {
    "vm_running": 1, "vm_total": 1, "lxc_running": 9, "lxc_total": 15,
    "vms":  [{"id": 100, "name": "haos", "status": "running", "node": "pve", "type": "qemu"}],
    "lxcs": [{"id": 103, "name": "pihole", "status": "running", "node": "pve", "type": "lxc"}]
  },
  "disks": [{"name": "nvme0n1", "state": "active", "temp_C": null},
            {"name": "sda", "state": "standby", "temp_C": null}],
  "ip": {"primary_ifname": "vmbr0", "primary_ipv4": "192.0.2.10/24", "gateway_ipv4": "192.0.2.1",
         "route_metric": null, "ip_status": "ok", "ipv4_addrs": [{"if": "vmbr0", "addr": "192.0.2.10/24"}]},
  "net": {"window_s": 120.0, "total_rx_Bps": 170585, "total_tx_Bps": 122182,
          "total_rx_bps": 1364680, "total_tx_bps": 977456,
          "interfaces": [{"if": "eno1", "rx_Bps": 170585, "tx_Bps": 122182, "virtual": false},
                         {"if": "vmbr0", "rx_Bps": 160000, "tx_Bps": 118000, "virtual": true}]}
}
```

| Field | Meaning |
|---|---|
| `ram.used_bytes` | total minus the kernel's `MemAvailable` |
| `net.window_s` | seconds since the previous request; rates are averages over it |
| `net.interfaces[].virtual` | `true` for anything without a device in sysfs (`lo`, bridges, `veth`, `tap`, bonds, VLANs) |
| `disks[].state` | `active` or `standby` (`null` if `hdparm` can't tell); NVMe is always `active` |
| `disks[].temp_C` | always `null` (reading it could wake the disk) |

Lines other than `GET` (the board's own log output arrives on the same
port) are ignored.

## Layout

| Path | What |
|---|---|
| `hostmon.py` | the service, one file, standard library + `pyserial`/`pyyaml` |
| `install_hostmon.sh`, `uninstall_hostmon.sh` | set up / remove it on a Debian-based host |
| `tests/` | pytest tests for everything that doesn't need a board or Proxmox |
| `.github/workflows/checks.yml` | ruff, tests on Python 3.11 and 3.13, shellcheck |

## Development

```bash
python3 -m venv ~/.venvs/thinklab-host
~/.venvs/thinklab-host/bin/pip install pyserial pyyaml pytest ruff
~/.venvs/thinklab-host/bin/ruff check . && ~/.venvs/thinklab-host/bin/ruff format --check .
~/.venvs/thinklab-host/bin/python -m pytest -q
```

A change to the JSON goes into the firmware too, with a new
`schema_version` if old firmware would misread it. See
[CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE)
