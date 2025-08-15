# ThinkLab Host (Host Monitor) — v1.1.26

Small host-side agent that streams Proxmox + system metrics to an ESP32 over serial.

## What’s in this release
- Only the `GET` command is handled (others are ignored silently).
- Disk temperatures disabled (`temp_C: null`); **no disk wake-ups**.
- Only base disks reported (`sdX`, `nvmeXnY`); partitions excluded.
- Lightweight CPU% & NET rate calculation with minimal CPU usage.
- Optional JSON tracing via installer `--trace` flag.

## Install (Proxmox/Debian)
```bash
# from the repo directory
sudo ./install_hostmon.sh --serial "/dev/serial/by-id/usb-....-if00"
# add --trace to enable request/response logs in journal
```

The installer will:
- Place files in `/opt/hl-hostmon/`
- Create a venv and install deps
- Write `/etc/hl-hostmon/config.yaml`
- Create and start `hl-hostmon.service`

## Uninstall
```bash
sudo ./uninstall_hostmon.sh
```
Config at `/etc/hl-hostmon/config.yaml` is preserved (delete manually if you want).

## Serial protocol (ESP32 side)
- Send `GET\n`
- Receive one line of JSON with this shape (example):
```json
{
  "schema_version":1,
  "script_version":"1.1.26",
  "timestamp_ms": 1700000000000,
  "hostname":"host",
  "uptime_s": 12345,
  "cpu":{"percent": 12.3, "cores": 16, "load1":0.42, "load5":0.50, "load15":0.66},
  "ram":{"total_bytes":..., "used_bytes":..., "swap_total_bytes":..., "swap_used_bytes":...},
  "filesystems":[{"mount":"/","total_bytes":...,"used_bytes":...}],
  "proxmox":{
    "vm_running":2,"vm_total":4,"lxc_running":1,"lxc_total":3,
    "vms":[{"id":100,"name":"vmname","status":"running","node":"host","type":"qemu"}],
    "lxcs":[{"id":101,"name":"ctname","status":"running","node":"host","type":"lxc"}]
  },
  "disks":[
    {"name":"nvme0n1","state":"active","temp_C":null},
    {"name":"sda","state":"standby","temp_C":null}
  ],
  "ip":{"primary_ifname":"vmbr0","primary_ipv4":"192.168.1.10/24","gateway_ipv4":"192.168.1.1","route_metric":null,"ip_status":"ok","ipv4_addrs":[{"if":"vmbr0","addr":"192.168.1.10/24"}]},
  "net":{"window_s":1,"total_rx_Bps":1234,"total_tx_Bps":5678,"total_rx_bps":9872,"total_tx_bps":45424,"interfaces":[{"if":"eno1","rx_Bps":1234,"tx_Bps":5678,"virtual":false}]}
}
```

## Config
`/etc/hl-hostmon/config.yaml`:
```yaml
serial_device: "/dev/serial/by-id/usb-...-if00"
baud: 115200
log_level: info        # set to debug with --trace
trace_payloads: false  # set to true with --trace
```

## License
MIT (add a LICENSE file if you haven't already).
