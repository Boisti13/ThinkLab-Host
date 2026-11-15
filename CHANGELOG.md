# Changelog

All notable changes, newest first. Each version is tagged `vX.Y.Z` on `main`.

## Unreleased

## v1.2.0 — 2026-11-15

### Fixed
- **Network totals** count physical NICs only (those with a device in
  sysfs). Before, `lo`, the bridge and every guest's `veth`/`tap` were added
  in, so traffic to a guest was counted two or three times (252 kB/s shown
  for 81 kB/s on the NIC). Interfaces are marked `virtual` the same way, so
  bonds and VLANs are now `virtual` too.
- **Memory used** is total minus `MemAvailable`; before, total minus free,
  buffers and cache, which counted shared memory as free.
- The **first answer after a start** has a CPU value: the counters are
  primed at start-up instead of on the first request.
- Installer: its error messages were never shown (they went into the
  captured device path); a by-id device without "tty" in its name wasn't
  found by the fallback.

### Changed
- **Logging**: start-up, the serial device and problems are logged at
  `info`; before, nothing at all was logged unless `log_level: debug`.
- After the serial port goes away, the service reopens it itself instead of
  exiting and waiting for systemd.

### Under the hood
- Tests (pytest), ruff, `pyproject.toml`, GitHub checks (Python 3.11 and
  3.13, shellcheck); `LICENSE` (MIT); README rewritten; `CLAUDE.md`.

## v1.1.26 — 2025-08-15

### Added
- The installer insists on exactly one ESP32: it prefers
  `/dev/serial/by-id/usb-Espressif_*` and stops if none or several are found.
- `WorkingDirectory` and `PYTHONUNBUFFERED=1` in the systemd unit.

### Changed
- The config gets the detected by-id path; `--serial` still overrides it.
- Installs as root without `sudo`.

### Fixed
- Container names come from `pct config` when `pct list` doesn't give one.
- `/dev/ttyACM0` is no longer picked when a by-id path exists.
