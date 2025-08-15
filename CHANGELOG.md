# Changelog

## 2025-08-15 — Host-Side v1.1.26

### Added
- Installer now enforces that **exactly one** ESP32 is connected before proceeding.
  - Prefers `/dev/serial/by-id/usb-Espressif_*` strict paths.
  - Fails if no ESP or more than one serial device is found.
- `WorkingDirectory` and `PYTHONUNBUFFERED=1` added to systemd unit for more predictable logs and execution context.

### Changed
- Config writer uses detected strict by-id path by default; manual override still possible with `--serial`.
- Install process streamlined for root environments (no `sudo`).

### Fixed
- Avoids mis-selection of `/dev/ttyACM0` when a strict `/dev/serial/by-id` path exists.
- Host script still ignores non-`GET` chatter; optional small patch available to hide `_Update_Full` even in `--trace` mode.
