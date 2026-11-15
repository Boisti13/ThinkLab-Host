"""Tests for the parts of hostmon.py that don't need a Proxmox host or a board."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hostmon  # noqa: E402

NETDEV_HEADER = (
    "Inter-|   Receive                                                |  Transmit\n"
    " face |bytes    packets errs drop fifo frame compressed multicast|"
    "bytes    packets errs drop fifo colls carrier compressed\n"
)


def netdev_line(name, rx, tx):
    return f"{name:>6}: {rx} 0 0 0 0 0 0 0 {tx} 0 0 0 0 0 0 0\n"


def write_netdev(path, counters):
    path.write_text(NETDEV_HEADER + "".join(netdev_line(n, rx, tx) for n, (rx, tx) in counters.items()))


def test_mem_info_uses_memavailable(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:       16000000 kB\n"
        "MemFree:         1000000 kB\n"
        "MemAvailable:    9000000 kB\n"
        "Buffers:          100000 kB\n"
        "Cached:          6000000 kB\n"
        "SwapTotal:       8000000 kB\n"
        "SwapFree:        7000000 kB\n"
    )
    total, used, swap_t, swap_u = hostmon.mem_info(str(meminfo))
    assert total == 16000000 * 1024
    assert used == (16000000 - 9000000) * 1024
    assert swap_t == 8000000 * 1024
    assert swap_u == 1000000 * 1024


def test_mem_info_without_memavailable_falls_back(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal: 1000 kB\nMemFree: 100 kB\nBuffers: 50 kB\nCached: 200 kB\nSReclaimable: 50 kB\n"
    )
    total, used, _, _ = hostmon.mem_info(str(meminfo))
    assert used == (1000 - 400) * 1024


def test_mem_info_missing_file_gives_none(tmp_path):
    assert hostmon.mem_info(str(tmp_path / "nope")) == (None, None, None, None)


def test_is_physical_if(tmp_path):
    (tmp_path / "eno1" / "device").mkdir(parents=True)
    (tmp_path / "vmbr0").mkdir()
    assert hostmon.is_physical_if("eno1", str(tmp_path))
    assert not hostmon.is_physical_if("vmbr0", str(tmp_path))
    assert not hostmon.is_physical_if("veth101i0", str(tmp_path))


def test_cpu_percent_after_priming(tmp_path):
    stat = tmp_path / "stat"
    stat.write_text("cpu  100 0 100 800 0 0 0 0 0 0\n")
    cpu = hostmon.CpuPercent(str(stat))
    assert cpu.compute() is None  # first call only primes
    stat.write_text("cpu  150 0 150 900 0 0 0 0 0 0\n")  # +100 busy, +100 idle
    assert cpu.compute() == 50.0


def test_net_rates(tmp_path, monkeypatch):
    netdev = tmp_path / "dev"
    write_netdev(netdev, {"eno1": (1000, 2000)})
    clock = iter([100.0, 102.0])
    monkeypatch.setattr(hostmon.time, "time", lambda: next(clock))
    net = hostmon.NetRates(str(netdev))
    net.rates()
    write_netdev(netdev, {"eno1": (3000, 6000)})
    dt, rates = net.rates()
    assert dt == pytest.approx(2.0)
    assert rates["eno1"] == (1000, 2000)


@pytest.fixture
def snapshot_env(tmp_path, monkeypatch):
    """A host with one NIC, a bridge, a guest veth and lo, all carrying the same traffic."""
    netdev = tmp_path / "dev"
    names = ["eno1", "vmbr0", "veth101i0", "lo"]
    write_netdev(netdev, {n: (0, 0) for n in names})
    now = [0.0]
    monkeypatch.setattr(hostmon.time, "time", lambda: now[0])
    net = hostmon.NetRates(str(netdev))
    net.rates()
    now[0] = 1.0
    write_netdev(netdev, {n: (500, 700) for n in names})

    stat = tmp_path / "stat"
    stat.write_text("cpu  1 0 1 8 0 0 0 0 0 0\n")
    cpu = hostmon.CpuPercent(str(stat))

    monkeypatch.setattr(hostmon, "proxmox_info", lambda: {"vm_running": 0, "vm_total": 0})
    monkeypatch.setattr(hostmon, "read_disks", lambda: [])
    monkeypatch.setattr(hostmon, "primary_ip", lambda: {})
    return cpu, net


def test_snapshot_totals_count_physical_only(snapshot_env):
    cpu, net = snapshot_env
    snap = hostmon.build_snapshot({}, cpu, net, physical=lambda ifn: ifn == "eno1")
    assert snap["schema_version"] == hostmon.SCHEMA
    assert snap["net"]["total_rx_Bps"] == 500
    assert snap["net"]["total_tx_Bps"] == 700
    assert snap["net"]["total_rx_bps"] == 4000
    virtual = {i["if"]: i["virtual"] for i in snap["net"]["interfaces"]}
    assert virtual == {"eno1": False, "lo": True, "veth101i0": True, "vmbr0": True}


def test_handle_line_answers_get_only(snapshot_env, monkeypatch):
    cpu, net = snapshot_env
    assert hostmon.handle_line("INFO", {}, cpu, net) is None
    assert hostmon.handle_line("E (123) wifi: error", {}, cpu, net) is None
    out = hostmon.handle_line("get", {}, cpu, net)
    assert "\n" not in out
    assert json.loads(out)["script_version"] == hostmon.VERSION
