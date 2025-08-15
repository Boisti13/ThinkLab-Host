#!/usr/bin/env python3
# ThinkLab Host Monitor - v1.1.26
# - Only handles "GET" (case-insensitive); ignores other serial inputs silently (debug trace optional)
# - Disk temps are disabled (temp_C=null always)
# - Partitions are excluded (list only base /sys/block devices: sd*, nvme*n*)
# - Low-CPU sampling: reuses last snapshots for CPU and NET to compute deltas
#
# Config file (/etc/hl-hostmon/config.yaml):
#   serial_device: "/dev/serial/by-id/..."
#   baud: 115200
#   log_level: info|debug
#   trace_payloads: false
#
import os, sys, time, json, re, subprocess, threading, queue, shutil, socket
from datetime import datetime

try:
    import yaml
except Exception:
    yaml = None

try:
    import serial  # pyserial
except Exception as e:
    print("[FATAL] pyserial not available: %s" % e, file=sys.stderr)
    sys.exit(1)

VERSION = "1.1.26"
SCHEMA = 1

# --------------- Utils -----------------
def read_yaml(path):
    if yaml is None:
        # tiny fallback: accept key: value lines only
        cfg = {}
        if os.path.exists(path):
            for ln in open(path, 'r', encoding='utf-8', errors='ignore'):
                m = re.match(r'^\s*([A-Za-z0-9_]+)\s*:\s*(.*?)\s*$', ln)
                if m:
                    k, v = m.group(1), m.group(2)
                    if v.lower() in ("true","false"): v = (v.lower()=="true")
                    elif v.isdigit(): v=int(v)
                    cfg[k]=v
        return cfg
    with open(path, 'r') as f:
        return yaml.safe_load(f) or {}

def sh(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        return ""

def now_ms():
    return int(time.time()*1000)

def uptime_seconds():
    try:
        with open("/proc/uptime","r") as f:
            return int(float(f.read().split()[0]))
    except:
        return None

# --------------- CPU percent (lightweight) -----------------
class CpuPercent:
    def __init__(self):
        self.prev_total = None
        self.prev_idle = None
        self.value = None

    def _read_stat(self):
        with open("/proc/stat","r") as f:
            ln = f.readline()  # cpu line
        parts = ln.split()
        vals = list(map(int, parts[1:]))  # user nice system idle iowait irq softirq steal guest guest_nice
        idle = vals[3] + (vals[4] if len(vals)>4 else 0)
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
        usage = max(0.0, min(100.0, (1.0 - (di/float(dt))) * 100.0))
        self.value = round(usage, 1)
        return self.value

# --------------- NET rates (lightweight) -----------------
class NetRates:
    def __init__(self):
        self.prev = None  # {if:(rx,tx)} with timestamp
        self.prev_t = None

    def _read(self):
        data={}
        with open("/proc/net/dev","r") as f:
            for ln in f.readlines()[2:]:
                if ":" not in ln: continue
                ifname, rest = ln.split(":",1)
                ifname = ifname.strip()
                cols = rest.split()
                rx = int(cols[0]); tx = int(cols[8])
                data[ifname]=(rx,tx)
        return data

    def rates(self):
        cur = self._read()
        t = time.time()
        if self.prev is None:
            self.prev, self.prev_t = cur, t
            # zero rates
            rates = {k:(0,0) for k in cur.keys()}
            return 1.0, rates
        dt = max(0.001, t - self.prev_t)
        rates={}
        for k,(rx,tx) in cur.items():
            pr = self.prev.get(k, (rx,tx))
            rates[k]= (int((rx-pr[0])/dt), int((tx-pr[1])/dt))
        self.prev, self.prev_t = cur, t
        return dt, rates

# --------------- Disks -----------------
def list_block_devices():
    devs = []
    for name in os.listdir("/sys/block"):
        # base devices are listed here. Exclude loop, ram, dm, md
        if re.match(r'^(loop|ram|dm-|md)', name): 
            continue
        # include sdX and nvme*n* only
        if re.match(r'^(sd[a-z]+)$', name) or re.match(r'^(nvme\d+n\d+)$', name):
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
    disks=[]
    for name in list_block_devices():
        st = disk_state(name)
        disks.append({"name": name, "state": st, "temp_C": None})
    return disks

# --------------- Proxmox -----------------
def proxmox_info():
    info={"vm_running":0,"vm_total":0,"lxc_running":0,"lxc_total":0,"vms":[],"lxcs":[]}
    qm = shutil.which("qm")
    pct = shutil.which("pct")
    node = socket.gethostname()
    # VMs
    if qm:
        out = sh("qm list --no-status --full 2>/dev/null || qm list 2>/dev/null")
        # Fallback parse: ID NAME STATUS
        for ln in out.splitlines():
            if not ln.strip() or ln.lower().startswith(("vmid","id")): 
                continue
            parts = ln.split()
            if len(parts) >= 3 and parts[0].isdigit():
                vmid = int(parts[0]); status = parts[-1].lower()
                name = " ".join(parts[1:-1])
                info["vms"].append({"id":vmid,"name":name or str(vmid),"status":status,"node":node,"type":"qemu"})
    # LXCs
    if pct:
        out = sh("pct list 2>/dev/null")
        for ln in out.splitlines():
            if not ln.strip() or ln.lower().startswith(("vmid","id")): 
                continue
            parts = ln.split()
            if len(parts) >= 3 and parts[0].isdigit():
                vmid = int(parts[0]); status = parts[-1].lower()
                name = " ".join(parts[1:-1])
                # Try name from config if numeric
                if name.isdigit():
                    cfg = sh(f"pct config {vmid} | grep -i '^hostname:'")
                    m = re.search(r'hostname:\s*(.+)$', cfg, re.IGNORECASE)
                    if m:
                        name = m.group(1).strip()
                info["lxcs"].append({"id":vmid,"name":name or str(vmid),"status":status,"node":node,"type":"lxc"})
    info["vm_total"] = len(info["vms"])
    info["lxc_total"] = len(info["lxcs"])
    info["vm_running"] = sum(1 for v in info["vms"] if v["status"]=="running")
    info["lxc_running"] = sum(1 for c in info["lxcs"] if c["status"]=="running")
    return info

# --------------- Snapshot -----------------
def mem_info():
    total=used=swap_t=swap_u=None
    try:
        m={}
        with open("/proc/meminfo","r") as f:
            for ln in f:
                k, v = ln.split(":",1)
                m[k.strip()] = int(v.strip().split()[0]) * 1024
        total = m.get("MemTotal")
        free = m.get("MemFree",0) + m.get("Buffers",0) + m.get("Cached",0) + m.get("SReclaimable",0)
        used = total - free if total is not None else None
        swap_t = m.get("SwapTotal"); swap_u = m.get("SwapTotal",0) - m.get("SwapFree",0)
    except:
        pass
    return total, used, swap_t, swap_u

def fs_root():
    st = os.statvfs("/")
    total = st.f_frsize * st.f_blocks
    used  = total - (st.f_frsize * st.f_bfree)
    return {"mount":"/","total_bytes":total,"used_bytes":used}

def primary_ip():
    # Default route and primary if
    route = sh("ip -4 route show default | head -n1")
    gw = None; dev=None
    m = re.search(r'default via ([0-9\.]+) dev (\S+)', route)
    if m:
        gw = m.group(1); dev=m.group(2)
    addrs=[]
    out = sh("ip -4 -o addr show")
    for ln in out.splitlines():
        cols = ln.split()
        ifname = cols[1]; cidr = cols[3]
        addrs.append({"if":ifname,"addr":cidr})
    prim_ip = None
    if dev:
        for a in addrs:
            if a["if"] == dev:
                prim_ip = a["addr"]
                break
    return {"primary_ifname":dev,"primary_ipv4":prim_ip,"gateway_ipv4":gw,"route_metric":None,"ip_status":"ok","ipv4_addrs":addrs}

def build_snapshot(cfg, cpu_calc, net_calc):
    cpu_pct = cpu_calc.compute()
    load1, load5, load15 = (0.0,0.0,0.0)
    try:
        with open("/proc/loadavg","r") as f:
            la = f.read().split()
            load1, load5, load15 = float(la[0]), float(la[1]), float(la[2])
    except:
        pass
    dt, rates = net_calc.rates()
    total_rx = sum(v[0] for v in rates.values())
    total_tx = sum(v[1] for v in rates.values())
    ifaces = []
    virt_if = set(["lo"])
    for ifn,(rx,tx) in sorted(rates.items()):
        virt = ifn in virt_if or ifn.startswith(("veth","tap","fw","vmbr"))
        ifaces.append({"if":ifn,"rx_Bps":rx,"tx_Bps":tx,"virtual":virt})
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
            "load1": load1, "load5": load5, "load15": load15
        },
        "ram": {
            "total_bytes": mem_t,
            "used_bytes":  mem_u,
            "swap_total_bytes": swap_t,
            "swap_used_bytes":  swap_u
        },
        "filesystems": [ fs_root() ],
        "proxmox": proxmox_info(),
        "disks": read_disks(),
        "ip": primary_ip(),
        "net": {
            "window_s": round(dt,3),
            "total_rx_Bps": total_rx,
            "total_tx_Bps": total_tx,
            "total_rx_bps": total_rx*8,
            "total_tx_bps": total_tx*8,
            "interfaces": ifaces
        }
    }
    return resp

# --------------- Serial I/O -----------------
def serve(cfg_path):
    cfg = read_yaml(cfg_path)
    serial_path = cfg.get("serial_device") or "/dev/ttyACM0"
    baud = int(cfg.get("baud", 115200))
    trace = bool(cfg.get("trace_payloads", False))
    log_level = (cfg.get("log_level","info") or "info").lower()

    cpu_calc = CpuPercent()
    net_calc = NetRates()

    # Wait for device if strict path is used
    while not os.path.exists(serial_path):
        if log_level == "debug":
            print(f"[DEBUG] waiting for serial {serial_path}...", flush=True)
        time.sleep(1)

    with serial.Serial(serial_path, baudrate=baud, timeout=1) as ser:
        if log_level == "debug":
            print(f"[INFO] Hostmon starting (v{VERSION}, schema {SCHEMA})", flush=True)
            print(f"[INFO] Config: {cfg_path} (baud={baud})", flush=True)
            print(f"[INFO] Using serial: {serial_path}", flush=True)
        while True:
            try:
                line = ser.readline().decode(errors="ignore").strip()
                if not line:
                    continue
                if trace and log_level == "debug":
                    print(f"[DEBUG] <- {line}", flush=True)
                cmd = line.strip().upper()
                if cmd == "GET":
                    payload = build_snapshot(cfg, cpu_calc, net_calc)
                    out = json.dumps(payload, separators=(',',':'))
                    if trace and log_level == "debug":
                        print(f"[DEBUG] -> {out}", flush=True)
                    ser.write((out+"\n").encode())
                else:
                    # ignore silently (optional tiny ack to avoid device waiting)
                    # ser.write(b'{}\n')  # comment out unless needed
                    pass
            except serial.SerialException:
                if log_level == "debug":
                    print("[WARN] Serial disconnected, waiting...", flush=True)
                # wait for path again
                while not os.path.exists(serial_path):
                    time.sleep(1)
                # reopen by breaking to outer with to re-enter
                time.sleep(1)
                return
            except Exception as e:
                if log_level == "debug":
                    print(f"[WARN] {e}", flush=True)
                time.sleep(0.1)

if __name__ == "__main__":
    cfg = "/etc/hl-hostmon/config.yaml"
    if len(sys.argv) > 1 and sys.argv[1] == "--config" and len(sys.argv) > 2:
        cfg = sys.argv[2]
    serve(cfg)
