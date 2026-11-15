# ThinkLab-Host — working rules

Host side of the ThinkLab e-ink display: `hostmon.py` answers `GET` lines
from the ESP32 on its USB serial port (CDC) with one line of JSON (CPU, RAM,
root filesystem, Proxmox guests, disks, IP, network). The firmware is the
sibling repo **ThinkLab-ESP**; the two share the JSON schema
(`schema_version`, currently 1). Change both together when the schema
changes, and keep the README's example payload current.

- **Public repo.** No IP addresses, MACs, hostnames of the owner's machines,
  serial numbers or by-id paths of real devices; use placeholders
  (`/dev/serial/by-id/usb-...-if00`). Where it actually runs is in the
  owner's private notes (`claude-setup/homelab.md`, wiki).
- One branch, `main`. Versions: `VERSION` in `hostmon.py`, the README's
  heading and `CHANGELOG.md` move together; the firmware's
  `HOST_SCRIPT_COMPAT` names the host version it was built against.
- **Installed copy**: `/opt/hl-hostmon/hostmon.py` (venv in
  `/opt/hl-hostmon/venv`), config `/etc/hl-hostmon/config.yaml`, service
  `hl-hostmon` (root). `install_hostmon.sh [--force] [--trace] [--serial
  <path>]` sets it up; it refuses unless exactly one Espressif serial
  device is present.
- **Deploying a change**: back up the installed script and config first
  (owner's backup place, see `homelab.md`), copy the committed
  `hostmon.py` over, `systemctl restart hl-hostmon`. The board polls every
  2 min, so wait one interval and check it still gets answers (with
  `log_level: info` nothing is logged: compare `wchar` in
  `/proc/<pid>/io` before and after a poll, or turn tracing on briefly).
- `--trace` / `log_level: debug` + `trace_payloads: true` writes every
  request and the full reply to the journal (≈ 1 MB/day): for debugging
  only, switch it back off afterwards. The board's own ESP-IDF log lines
  arrive on the same port and show up in the trace as `<- …`.
- Keep it light: it runs on a Proxmox host every 2 min. No disk wake-ups
  (`hdparm -C` only, no SMART on spun-down disks), no new dependencies
  beyond `pyyaml` and `pyserial` without a reason.
- No tests yet; at least `python3 -m py_compile hostmon.py` and one real
  `GET` against the board (or a pty pair) before pushing.
- README and CHANGELOG follow the owner's house style (badges, highlights,
  tables; `## vX.Y.Z — date`, Added / Changed / Fixed / Under the hood,
  *Unreleased* on top); update both with every user-visible change.
