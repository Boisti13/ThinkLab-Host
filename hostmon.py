#!/usr/bin/env python3
# ThinkLab Host Monitor - v1.2.0
# - Only handles "GET" (case-insensitive); ignores other serial inputs silently (debug trace optional)
# - Disk temps are disabled (temp_C=null always)
# - Partitions are excluded (list only base /sys/block devices: sd*, nvme*n*)
# - Low-CPU sampling: reuses last snapshots for CPU and NET to compute deltas
# - Network totals count physical interfaces only (those with /sys/class/net/<if>/device)
#
# Config file (/etc/hl-hostmon/config.yaml):
#   serial_device: "/dev/serial/by-id/..."
#   baud: 115200
#   log_level: info|debug
#   trace_payloads: false
#
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

try:
    import yaml
except Exception:
    yaml = None

try:
    import serial  # pyserial
except Exception as e:
    print(f"[FATAL] pyserial not available: {e}", file=sys.stderr)
    sys.exit(1)

VERSION = "1.2.0"
SCHEMA = 1

# --------------- Utils -----------------
LEVELS = {"debug": 10, "info": 20, "warn": 30}
_log_level = LEVELS["info"]


def set_log_level(name):
    global _log_level
    _log_level = LEVELS.get((name or "info").lower(), LEVELS["info"])


def log(level, msg):
    """Print a log line if `level` (debug/info/warn) is at or above the configured level."""
    if LEVELS[level] >= _log_level:
        print(f"[{level.upper()}] {msg}", flush=True)


def read_yaml(path):
    if yaml is None:
        # tiny fallback: accept key: value lines only
        cfg = {}
        if os.path.exists(path):
            for ln in open(path, encoding="utf-8", errors="ignore"):
                m = re.match(r"^\s*([A-Za-z0-9_]+)\s*:\s*(.*?)\s*$", ln)
                if m:
                    k, v = m.group(1), m.group(2)
                    if v.lower() in ("true", "false"):
                        v = v.lower() == "true"
                    elif v.isdigit():
                        v = int(v)
                    cfg[k] = v
        return cfg
    with open(path) as f:
        return yaml.safe_load(f) or {}


def sh(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        return ""


def now_ms():
    return int(time.time() * 1000)


def uptime_seconds():
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


# --------------- CPU percent (lightweight) -----------------
class CpuPercent:
    def __init__(self, stat_path="/proc/stat"):
        self.stat_path = stat_path
        self.prev_total = None
        self.prev_idle = None
        self.value = None

    def _read_stat(self):
        with open(self.stat_path) as f:
            ln = f.readline()  # cpu line
        parts = ln.split()
        vals = list(map(int, parts[1:]))  # user nice system idle iowait irq softirq steal guest guest_nice
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals[:8])
        return total, idle

    def compute(self):
        total, idle = self._read_stat()
        if self.prev_total is None:
            self.prev_total, self.prev_idle = total, idle
            self.value = None
            return None
        dt = total - self.prev_total
        di = idle - self.prev_idle
        self.prev_total, self.prev_idle = total, idle
        if dt <= 0:
            self.value = None
            return None
        usage = max(0.0, min(100.0, (1.0 - (di / float(dt))) * 100.0))
        self.value = round(usage, 1)
        return self.value


# --------------- NET rates (lightweight) -----------------
class NetRates:
    def __init__(self, netdev_path="/proc/net/dev"):
        self.netdev_path = netdev_path
        self.prev = None  # {if:(rx,tx)} with timestamp
        self.prev_t = None

    def _read(self):
        data = {}
        with open(self.netdev_path) as f:
            for ln in f.readlines()[2:]:
                if ":" not in ln:
                    continue
                ifname, rest = ln.split(":", 1)
                ifname = ifname.strip()
                cols = rest.split()
                rx = int(cols[0])
                tx = int(cols[8])
                data[ifname] = (rx, tx)
        return data

    def rates(self):
        cur = self._read()
        t = time.time()
        if self.prev is None:
            self.prev, self.prev_t = cur, t
            # zero rates
            rates = {k: (0, 0) for k in cur.keys()}
            return 1.0, rates
        dt = max(0.001, t - self.prev_t)
        rates = {}
        for k, (rx, tx) in cur.items():
            pr = self.prev.get(k, (rx, tx))
            rates[k] = (int((rx - pr[0]) / dt), int((tx - pr[1]) / dt))
        self.prev, self.prev_t = cur, t
        return dt, rates


# --------------- Disks -----------------
def list_block_devices():
    devs = []
    for name in os.listdir("/sys/block"):
        # base devices are listed here. Exclude loop, ram, dm, md
        if re.match(r"^(loop|ram|dm-|md)", name):
            continue
        # include sdX and nvme*n* only
        if re.match(r"^(sd[a-z]+)$", name) or re.match(r"^(nvme\d+n\d+)$", name):
            devs.append(name)
    return sorted(devs)


def disk_state(devname):
    # NVMe: consider active (we avoid waking/poking power state)
    if devname.startswith("nvme"):
        return "active"
    # SATA/SAS: hdparm -C (non-waking)
    path = f"/dev/{devname}"
    out = sh(f"hdparm -C {path}")
    m = re.search(r"drive state is:\s+(.*)", out)
    if not m:
        return None
    state = m.group(1).strip().lower()
    if "standby" in state:
        return "standby"
    if "sleep" in state:
        return "standby"
    if "active" in state or "idle" in state:
        return "active"
    return None


def read_disks():
    disks = []
    for name in list_block_devices():
        st = disk_state(name)
        disks.append({"name": name, "state": st, "temp_C": None})
    return disks


# --------------- Proxmox -----------------
def proxmox_info():
    """
    Prefer pvesh JSON (no fragile columns). Fallbacks handle swapped column orders.
    Returns:
      {"vm_running":int,"vm_total":int,"lxc_running":int,"lxc_total":int,
       "vms":[{id,name,status,node,type:"qemu"}],
       "lxcs":[{id,name,status,node,type:"lxc"}]}
    """

    def _sh(cmd):
        return subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.DEVNULL).strip()

    node = socket.gethostname()
    info = {
        "vm_running": 0,
        "vm_total": 0,
        "lxc_running": 0,
        "lxc_total": 0,
        "vms": [],
        "lxcs": [],
    }

    # --- Preferred: pvesh JSON
    if shutil.which("pvesh"):
        try:
            out = _sh("pvesh get /cluster/resources --type vm --output-format json")
            data = json.loads(out)
            for it in data:
                typ = it.get("type")  # "qemu" or "lxc"
                vmid = it.get("vmid")
                name = it.get("name") or str(vmid)
                status = it.get("status", "unknown")
                n = it.get("node", node)
                if typ == "qemu":
                    info["vms"].append(
                        {
                            "id": vmid,
                            "name": name,
                            "status": status,
                            "node": n,
                            "type": "qemu",
                        }
                    )
                elif typ == "lxc":
                    info["lxcs"].append(
                        {
                            "id": vmid,
                            "name": name,
                            "status": status,
                            "node": n,
                            "type": "lxc",
                        }
                    )
            info["vms"].sort(key=lambda x: x["id"])
            info["lxcs"].sort(key=lambda x: x["id"])
            info["vm_total"] = len(info["vms"])
            info["lxc_total"] = len(info["lxcs"])
            info["vm_running"] = sum(1 for v in info["vms"] if v["status"] == "running")
            info["lxc_running"] = sum(1 for c in info["lxcs"] if c["status"] == "running")
            return info
        except Exception:
            pass  # fall back

    # --- Fallback: qm list + qm status
    if shutil.which("qm"):
        try:
            out = _sh("qm list --no-status --full 2>/dev/null || qm list 2>/dev/null")
            lines = [ln for ln in out.splitlines() if ln.strip()]
            data_lines = [ln for ln in lines if not re.match(r"^\s*(VMID|ID)\b", ln, re.IGNORECASE)]
            for ln in data_lines:
                parts = ln.split()
                if not parts or not parts[0].isdigit():
                    continue
                vmid = int(parts[0])
                # Heuristic: name is tokens after VMID until first pure number/float
                name_tokens = []
                for tok in parts[1:]:
                    if re.fullmatch(r"\d+(\.\d+)?", tok):
                        break
                    name_tokens.append(tok)
                name = " ".join(name_tokens).strip() or str(vmid)
                try:
                    st = _sh(f"qm status {vmid} | awk '/status:/{{print $2}}'")
                    status = st.strip().lower() or "unknown"
                except Exception:
                    status = "unknown"
                info["vms"].append(
                    {
                        "id": vmid,
                        "name": name,
                        "status": status,
                        "node": node,
                        "type": "qemu",
                    }
                )
        except Exception:
            pass

    # --- Fallback: pct list (header-aware; handles swapped status/name)
    if shutil.which("pct"):
        try:
            out = _sh("pct list 2>/dev/null")
            lines = [ln for ln in out.splitlines() if ln.strip()]
            if lines:
                hdr = lines[0].lower().split()
                idx_id = next((i for i, t in enumerate(hdr) if t in ("vmid", "id")), None)
                idx_status = next((i for i, t in enumerate(hdr) if t == "status"), None)
                idx_name = next((i for i, t in enumerate(hdr) if t == "name"), None)

                for ln in lines[1:]:
                    parts = ln.split()
                    if not parts or idx_id is None or len(parts) <= idx_id or not parts[idx_id].isdigit():
                        continue
                    vmid = int(parts[idx_id])

                    status = "unknown"
                    name = ""
                    if idx_status is not None and len(parts) > idx_status:
                        status = parts[idx_status].lower()
                    if idx_name is not None and len(parts) > idx_name:
                        name = parts[idx_name]

                    # Handle layout like: "VMID running palmr" (status then name)
                    if name in (
                        "running",
                        "stopped",
                        "paused",
                        "unknown",
                    ) and status not in ("running", "stopped", "paused"):
                        name, status = status, name

                    if not name or name.isdigit():
                        try:
                            cfg = _sh(f"pct config {vmid} | grep -i '^hostname:' | awk '{{print $2}}'")
                            if cfg.strip():
                                name = cfg.strip()
                        except Exception:
                            pass
                    if not name:
                        name = str(vmid)

                    info["lxcs"].append(
                        {
                            "id": vmid,
                            "name": name,
                            "status": status,
                            "node": node,
                            "type": "lxc",
                        }
                    )
        except Exception:
            pass

    info["vms"].sort(key=lambda x: x["id"])
    info["lxcs"].sort(key=lambda x: x["id"])
    info["vm_total"] = len(info["vms"])
    info["lxc_total"] = len(info["lxcs"])
    info["vm_running"] = sum(1 for v in info["vms"] if v["status"] == "running")
    info["lxc_running"] = sum(1 for c in info["lxcs"] if c["status"] == "running")
    return info


# --------------- Snapshot -----------------
def is_physical_if(ifname, sys_class_net="/sys/class/net"):
    """A real NIC has a `device` link in sysfs; lo, bridges, veth, tap, fw*, bonds and VLANs don't."""
    return os.path.exists(os.path.join(sys_class_net, ifname, "device"))


def mem_info(meminfo_path="/proc/meminfo"):
    total = used = swap_t = swap_u = None
    try:
        m = {}
        with open(meminfo_path) as f:
            for ln in f:
                k, v = ln.split(":", 1)
                m[k.strip()] = int(v.strip().split()[0]) * 1024
        total = m.get("MemTotal")
        if "MemAvailable" in m:
            avail = m["MemAvailable"]
        else:  # kernels before 3.14
            avail = m.get("MemFree", 0) + m.get("Buffers", 0) + m.get("Cached", 0) + m.get("SReclaimable", 0)
        used = total - avail if total is not None else None
        swap_t = m.get("SwapTotal")
        swap_u = m.get("SwapTotal", 0) - m.get("SwapFree", 0)
    except (OSError, ValueError):
        pass
    return total, used, swap_t, swap_u


def fs_root():
    st = os.statvfs("/")
    total = st.f_frsize * st.f_blocks
    used = total - (st.f_frsize * st.f_bfree)
    return {"mount": "/", "total_bytes": total, "used_bytes": used}


def primary_ip():
    # Default route and primary if
    route = sh("ip -4 route show default | head -n1")
    gw = None
    dev = None
    m = re.search(r"default via ([0-9\.]+) dev (\S+)", route)
    if m:
        gw = m.group(1)
        dev = m.group(2)
    addrs = []
    out = sh("ip -4 -o addr show")
    for ln in out.splitlines():
        cols = ln.split()
        ifname = cols[1]
        cidr = cols[3]
        addrs.append({"if": ifname, "addr": cidr})
    prim_ip = None
    if dev:
        for a in addrs:
            if a["if"] == dev:
                prim_ip = a["addr"]
                break
    return {
        "primary_ifname": dev,
        "primary_ipv4": prim_ip,
        "gateway_ipv4": gw,
        "route_metric": None,
        "ip_status": "ok",
        "ipv4_addrs": addrs,
    }


def build_snapshot(cfg, cpu_calc, net_calc, physical=None):
    physical = physical or is_physical_if
    cpu_pct = cpu_calc.compute()
    load1, load5, load15 = (0.0, 0.0, 0.0)
    try:
        with open("/proc/loadavg") as f:
            la = f.read().split()
            load1, load5, load15 = float(la[0]), float(la[1]), float(la[2])
    except (OSError, ValueError, IndexError):
        pass
    dt, rates = net_calc.rates()
    # Totals over physical NICs only: a packet to a guest passes the NIC, the
    # bridge and the guest's veth/tap, so summing everything counts it 2-3 times.
    ifaces = []
    total_rx = total_tx = 0
    for ifn, (rx, tx) in sorted(rates.items()):
        virt = not physical(ifn)
        ifaces.append({"if": ifn, "rx_Bps": rx, "tx_Bps": tx, "virtual": virt})
        if not virt:
            total_rx += rx
            total_tx += tx
    mem_t, mem_u, swap_t, swap_u = mem_info()
    resp = {
        "schema_version": SCHEMA,
        "script_version": VERSION,
        "timestamp_ms": now_ms(),
        "hostname": socket.gethostname(),
        "uptime_s": uptime_seconds(),
        "cpu": {
            "percent": cpu_pct,
            "cores": os.cpu_count() or None,
            "load1": load1,
            "load5": load5,
            "load15": load15,
        },
        "ram": {
            "total_bytes": mem_t,
            "used_bytes": mem_u,
            "swap_total_bytes": swap_t,
            "swap_used_bytes": swap_u,
        },
        "filesystems": [fs_root()],
        "proxmox": proxmox_info(),
        "disks": read_disks(),
        "ip": primary_ip(),
        "net": {
            "window_s": round(dt, 3),
            "total_rx_Bps": total_rx,
            "total_tx_Bps": total_tx,
            "total_rx_bps": total_rx * 8,
            "total_tx_bps": total_tx * 8,
            "interfaces": ifaces,
        },
    }
    return resp


# --------------- Serial I/O -----------------
def handle_line(line, cfg, cpu_calc, net_calc, trace=False):
    """Return the reply for one line from the board, or None to stay silent."""
    if trace:
        log("debug", f"<- {line}")
    if line.strip().upper() != "GET":
        return None
    out = json.dumps(build_snapshot(cfg, cpu_calc, net_calc), separators=(",", ":"))
    if trace:
        log("debug", f"-> {out}")
    return out


def serve_once(cfg, cfg_path, cpu_calc, net_calc):
    """Open the port and answer requests until it goes away."""
    serial_path = cfg.get("serial_device") or "/dev/ttyACM0"
    baud = int(cfg.get("baud", 115200))
    trace = bool(cfg.get("trace_payloads", False))

    waited = False
    while not os.path.exists(serial_path):
        if not waited:
            log("info", f"Waiting for serial device {serial_path}")
            waited = True
        time.sleep(1)

    with serial.Serial(serial_path, baudrate=baud, timeout=1) as ser:
        log("info", f"Using serial: {serial_path} ({baud} baud)")
        while True:
            try:
                line = ser.readline().decode(errors="ignore").strip()
                if not line:
                    continue
                out = handle_line(line, cfg, cpu_calc, net_calc, trace)
                if out is not None:
                    ser.write((out + "\n").encode())
            except serial.SerialException as e:
                log("warn", f"Serial disconnected ({e}), reopening")
                time.sleep(1)
                return
            except Exception as e:
                log("warn", f"{type(e).__name__}: {e}")
                time.sleep(0.1)


def serve(cfg_path):
    cfg = read_yaml(cfg_path)
    set_log_level(cfg.get("log_level", "info"))
    log("info", f"Hostmon starting (v{VERSION}, schema {SCHEMA}), config {cfg_path}")

    cpu_calc = CpuPercent()
    net_calc = NetRates()
    # Prime the delta counters so the first reply already has CPU and rates.
    cpu_calc.compute()
    net_calc.rates()

    while True:
        serve_once(cfg, cfg_path, cpu_calc, net_calc)


if __name__ == "__main__":
    cfg = "/etc/hl-hostmon/config.yaml"
    if len(sys.argv) > 1 and sys.argv[1] == "--config" and len(sys.argv) > 2:
        cfg = sys.argv[2]
    serve(cfg)
