"""
AXIOM Pi Agent — v1.1
─────────────────────────────────────────────────
Full parity with main AXIOM sidebar:
  System · Kernel · Auth · Docker · Disk · Boot · S.M.A.R.T · Sysmon

Endpoints:
  GET /health
  GET /api/metrics
  GET /api/sources
  GET /api/logs/{log_type}   — syslog|kernel|auth|docker|disk|boot|smart
  GET /api/sysmon-logs
  GET /api/analyze/{log_type}
"""

import os
import re
import json
import time
import subprocess
import collections
from pathlib import Path
from datetime import datetime
from typing import Optional

import psutil
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# ── Config ───────────────────────────────────────────────────────────────────
AGENT_VERSION = "1.1"
NODE_NAME     = os.getenv("NODE_NAME",   "raspberry-pi")
NODE_ROLE     = os.getenv("NODE_ROLE",   "agent")
OLLAMA_HOST   = os.getenv("OLLAMA_HOST", "").rstrip("/")
OLLAMA_MODEL  = os.getenv("OLLAMA_MODEL", "llama3.2:1b")
LOG_ROOT      = Path("/var/log")
BOOT_TIME     = psutil.boot_time()

app = FastAPI(title="AXIOM-Pi-Agent", version=AGENT_VERSION, docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── App log ──────────────────────────────────────────────────────────────────
_APP_LOG: collections.deque = collections.deque(maxlen=500)

def _app_log(msg: str, level: str = "INFO"):
    entry = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}"
    _APP_LOG.append(entry)
    print(entry, flush=True)

_app_log(f"AXIOM Pi Agent v{AGENT_VERSION} starting — node:{NODE_NAME} ollama:{OLLAMA_HOST or 'not set'}")

# ── Helpers ──────────────────────────────────────────────────────────────────

def _run(cmd: list, timeout: int = 10) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
        return r.stdout
    except Exception:
        return ""

def _fmt_uptime(s: int) -> str:
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:  return f"{d}d {h}h {m}m"
    if h:  return f"{h}h {m}m"
    return f"{m}m {s}s"

def _fmt_bytes(b: float) -> str:
    for unit in ("B","KB","MB","GB","TB"):
        if b < 1024: return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"

# ── Pi-specific ──────────────────────────────────────────────────────────────

def _pi_temp() -> Optional[float]:
    for path in [
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/devices/virtual/thermal/thermal_zone0/temp",
    ]:
        try:
            return round(int(Path(path).read_text().strip()) / 1000, 1)
        except Exception:
            pass
    try:
        sensors = psutil.sensors_temperatures()
        for key in ("cpu_thermal", "coretemp", "acpitz"):
            if key in sensors and sensors[key]:
                return round(sensors[key][0].current, 1)
    except Exception:
        pass
    return None

def _pi_model() -> str:
    try:
        return Path("/proc/device-tree/model").read_text().strip().rstrip("\x00")
    except Exception:
        pass
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("Model"):
                return line.split(":", 1)[-1].strip()
    except Exception:
        pass
    return "Raspberry Pi"

def _pi_throttle() -> dict:
    """
    Tries vcgencmd first, then falls back to sysfs (works in privileged container).
    Bits: 0x1=under-voltage  0x4=throttled  0x8=soft-temp-limit
    """
    status = {"throttled": False, "under_voltage": False, "soft_temp_limit": False, "raw": None}

    out = _run(["vcgencmd", "get_throttled"])
    if out.strip() and "=" in out:
        status["raw"] = out.strip()
        try:
            val = int(out.strip().split("=")[-1], 16)
            status["throttled"]       = bool(val & 0x4)
            status["under_voltage"]   = bool(val & 0x1)
            status["soft_temp_limit"] = bool(val & 0x8)
        except Exception:
            pass
        return status

    for sysfs_path in [
        "/sys/devices/platform/soc/soc:firmware/get_throttled",
        "/sys/devices/platform/raspberrypi-firmware/get_throttled",
    ]:
        try:
            raw = Path(sysfs_path).read_text().strip()
            if raw:
                status["raw"] = raw
                val = int(raw, 16)
                status["throttled"]       = bool(val & 0x4)
                status["under_voltage"]   = bool(val & 0x1)
                status["soft_temp_limit"] = bool(val & 0x8)
                return status
        except Exception:
            pass
    return status

def _pi_freq() -> dict:
    out = _run(["vcgencmd", "measure_clock", "arm"])
    if out.strip() and "=" in out:
        try:
            return {"arm_mhz": round(int(out.strip().split("=")[-1]) / 1e6, 0)}
        except Exception:
            pass
    try:
        f = psutil.cpu_freq()
        if f: return {"arm_mhz": round(f.current, 0)}
    except Exception:
        pass
    return {"arm_mhz": None}

# ── Metrics ──────────────────────────────────────────────────────────────────

@app.get("/api/metrics")
async def get_metrics():
    cpu      = psutil.cpu_percent(interval=0.3)
    mem      = psutil.virtual_memory()
    swap     = psutil.swap_memory()
    net_io   = psutil.net_io_counters()
    uptime_s = int(time.time() - BOOT_TIME)
    load1, load5, load15 = psutil.getloadavg()

    # Read host disk partitions from /proc/1/mounts (host mount namespace).
    # psutil.disk_partitions() only sees the container namespace — useless here.
    _MOUNT_OK = re.compile(r"^(/$|/boot(/firmware)?|/mnt/|/media/|/home/|/opt/|/data/|/storage/)")
    _SKIP_FS  = re.compile(r"^(overlay|tmpfs|devtmpfs|shm|cgroup|proc|sysfs|none|squashfs|autofs)")
    raw_mounts = []
    for mounts_file in ["/proc/1/mounts", "/proc/mounts"]:
        try:
            with open(mounts_file) as f:
                raw_mounts = f.readlines()
            if raw_mounts:
                break
        except Exception:
            pass
    seen: set = set()
    disk_parts = []
    for line in raw_mounts:
        cols = line.split()
        if len(cols) < 3:
            continue
        dev, mp, fstype = cols[0], cols[1], cols[2]
        if not dev.startswith("/dev/"):
            continue
        if _SKIP_FS.match(fstype):
            continue
        if not _MOUNT_OK.match(mp):
            continue
        if mp in seen:
            continue
        seen.add(mp)
        try:
            u = psutil.disk_usage(mp)
            disk_parts.append({
                "mount": mp, "device": dev, "fstype": fstype,
                "total": u.total, "used": u.used, "free": u.free, "percent": u.percent,
            })
        except Exception:
            pass

    # Normalise disk entries to _gb fields (mirrors main.py format)
    disks_normalised = []
    for d in disk_parts:
        disks_normalised.append({
            "mount":    d["mount"],
            "device":   d.get("device"),
            "fstype":   d.get("fstype"),
            "total_gb": round(d["total"] / 1e9, 1),
            "used_gb":  round(d["used"]  / 1e9, 1),
            "free_gb":  round(d["free"]  / 1e9, 1),
            "percent":  d["percent"],
        })

    # Normalise freq: expose current_mhz so fleet dashboard works uniformly
    raw_freq = _pi_freq()
    freq_normalised = None
    if raw_freq and raw_freq.get("arm_mhz") is not None:
        freq_normalised = {"current_mhz": raw_freq["arm_mhz"], "arm_mhz": raw_freq["arm_mhz"]}

    return {
        "node":         NODE_NAME,
        "role":         NODE_ROLE,
        "model":        _pi_model(),
        "uptime_s":     uptime_s,
        "uptime_human": _fmt_uptime(uptime_s),
        "cpu_percent":  cpu,
        "cpu_count":    psutil.cpu_count(logical=True),
        # Flat load fields — mirrors main.py format
        "load_1":  round(load1,  2),
        "load_5":  round(load5,  2),
        "load_15": round(load15, 2),
        # Keep nested load for any existing callers
        "load": {"1m": round(load1,2), "5m": round(load5,2), "15m": round(load15,2)},
        "memory": {
            # _gb fields — mirrors main.py format
            "total_gb": round(mem.total / 1e9, 1),
            "used_gb":  round(mem.used  / 1e9, 1),
            "free_gb":  round(mem.free  / 1e9, 1),
            "percent":  mem.percent,
            # Raw bytes kept for any existing callers
            "total":   mem.total,
            "used":    mem.used,
            "free":    mem.free,
            "cached":  getattr(mem, "cached", 0),
            "buffers": getattr(mem, "buffers", 0),
        },
        "swap": {
            "total_gb": round(swap.total / 1e9, 1),
            "used_gb":  round(swap.used  / 1e9, 1),
            "percent":  swap.percent,
            "total":    swap.total,
            "used":     swap.used,
            "free":     swap.free,
        },
        "disks": disks_normalised,
        "network": {
            "bytes_sent":    net_io.bytes_sent,
            "bytes_recv":    net_io.bytes_recv,
            "packets_sent":  net_io.packets_sent,
            "packets_recv":  net_io.packets_recv,
        },
        "temperature_c": _pi_temp(),
        "throttle":      _pi_throttle(),
        "freq":          freq_normalised,
        "fetched_at":    datetime.now().isoformat(),
    }

# ── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok", "version": AGENT_VERSION,
        "node": NODE_NAME, "role": NODE_ROLE,
        "log_root": str(LOG_ROOT), "log_exists": LOG_ROOT.exists(),
    }

# ── Sysmon ───────────────────────────────────────────────────────────────────

@app.get("/api/sysmon-logs")
async def get_sysmon_logs():
    return {"lines": list(_APP_LOG), "count": len(_APP_LOG)}

# ── Log sources ──────────────────────────────────────────────────────────────

LOG_SOURCES = {
    "syslog": {
        "label": "System", "desc": "Daemons · kernel events",
        # journald-first — this Pi has no syslog file, only journald
        "files": ["syslog", "messages", "system.log"],
        "pattern": None,
        "journal_args": [],
        "journal_first": True,
    },
    "kernel": {
        "label": "Kernel", "desc": "Hardware · drivers",
        "files": ["kern.log", "kern.log.1", "messages"],
        "pattern": None,
        "journal_args": ["-k"],
        "journal_first": True,
    },
    "auth": {
        "label": "Auth / Security", "desc": "SSH · sudo · logins",
        "files": ["auth.log", "auth.log.1", "secure"],
        "pattern": None,
        "journal_args": ["-t", "sudo", "-t", "sshd", "-t", "pam", "-t", "login"],
        "journal_first": True,
    },
    "docker": {
        "label": "Docker", "desc": "Container logs",
        "files": [],
        "pattern": r"(docker|containerd|container)",
        "journal_args": ["-u", "docker", "-u", "containerd"],
        "journal_first": True,
    },
    "disk": {
        "label": "Disk / Storage", "desc": "Storage · I/O",
        "files": ["syslog", "kern.log", "messages"],
        "pattern": r"(sd[a-z]|mmcblk|nvme|EXT4|XFS|BTRFS|I/O error|blk_|scsi)",
        "journal_args": ["-k"],
        "journal_first": True,
    },
    "boot": {
        "label": "Boot", "desc": "Boot · dmesg",
        "files": ["boot.log", "dmesg"],
        "pattern": None,
        "journal_args": ["-b"],
        "journal_first": True,
    },
    "casaos": {
        "label": "CasaOS", "desc": "CasaOS app & service logs",
        "files": [],
        "pattern": None,
        "journal_args": ["-u", "casaos", "-u", "casaos-gateway", "-u", "casaos-user-service",
                         "-u", "casaos-app-management", "-u", "runit"],
        "journal_first": True,
        "casaos_log_dir": "/var/log/casaos",
    },
}

JOURNAL_DIRS = ["/run/log/journal", "/var/log/journal"]

def _filter(lines: list, pattern: Optional[str]) -> list:
    if not pattern:
        return lines
    rx = re.compile(pattern, re.IGNORECASE)
    return [l for l in lines if rx.search(l)]

def read_log_tail(filename: str, lines: int, pattern=None) -> list:
    path = LOG_ROOT / filename
    if not path.exists():
        return []
    try:
        fetch = lines * 4 if pattern else lines
        r = subprocess.run(["tail", "-n", str(fetch), str(path)],
                           capture_output=True, text=True, timeout=10, errors="replace")
        return _filter(r.stdout.splitlines(), pattern)[-lines:]
    except Exception:
        return []

def read_journal(lines: int, args: Optional[list] = None, pattern=None) -> list:
    """Read from journald. Calls journalctl directly without -D — works inside
    privileged containers where the host journal is accessible via /run/log/journal."""
    fetch = lines * 4 if pattern else lines
    cmd = (["journalctl", "--no-pager", "-q", "--output=short-iso", f"-n{fetch}"]
           + (args or []))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15, errors="replace")
        out = [l for l in r.stdout.splitlines() if l and not l.startswith("--")]
        if out:
            return _filter(out, pattern)[-lines:]
    except FileNotFoundError:
        _app_log("journalctl not found", "WARN")
    except Exception as e:
        _app_log(f"journalctl error: {e}", "WARN")
    return []

def read_dmesg(lines: int, pattern=None) -> list:
    for cmd in [["dmesg", "-T"], ["dmesg"]]:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=12, errors="replace")
            out = r.stdout.splitlines()
            if out:
                return _filter(out, pattern)[-lines:]
        except Exception:
            continue
    return []

def read_docker_logs(lines: int) -> list:
    sock = Path("/var/run/docker.sock")
    if sock.exists():
        try:
            ps = _run(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"])
            containers = [l.split("\t")[0] for l in ps.splitlines() if l.strip()]
            results = []
            for name in containers[:8]:
                logs = _run(["docker", "logs", "--tail", "30", "--timestamps", name])
                for line in logs.splitlines()[-30:]:
                    results.append(f"[{name}] {line}")
            if results:
                return results[-lines:]
        except Exception:
            pass
    # fallback: journalctl
    return read_journal(lines, ["-u", "docker", "-u", "containerd"])

def read_casaos_logs(lines: int) -> list:
    """Read CasaOS log files directly. Files are root-owned so privileged is required."""
    results = []
    # Main CasaOS service logs
    log_dir = Path("/var/log/casaos")
    if log_dir.exists():
        for logfile in sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]:
            try:
                r = subprocess.run(["tail", "-n", str(max(lines // 3, 50)), str(logfile)],
                                   capture_output=True, text=True, timeout=8, errors="replace")
                for line in r.stdout.splitlines():
                    if line.strip():
                        results.append(f"[{logfile.stem}] {line}")
            except Exception as e:
                results.append(f"[{logfile.stem}] [read error: {e}]")
    # Runit service logs (e.g. SSH)
    runit_dir = Path("/var/log/runit")
    if runit_dir.exists():
        for svc_dir in list(runit_dir.iterdir())[:3]:
            current = svc_dir / "current"
            if current.exists():
                try:
                    r = subprocess.run(["tail", "-n", "30", str(current)],
                                       capture_output=True, text=True, timeout=5, errors="replace")
                    for line in r.stdout.splitlines():
                        if line.strip():
                            results.append(f"[runit/{svc_dir.name}] {line}")
                except Exception:
                    pass
    return results[-lines:] if results else []


def read_log_source(log_type: str, lines: int) -> list:
    if log_type not in LOG_SOURCES:
        return [f"[Unknown log type: {log_type}]"]
    src     = LOG_SOURCES[log_type]
    pattern = src.get("pattern")

    # Docker: try socket first (needs docker CLI), then journal
    if log_type == "docker":
        result = read_docker_logs(lines)
        if result:
            return result
        # journal fallback for docker
        result = read_journal(lines, src.get("journal_args", []), pattern)
        if result:
            return result
        # last resort: dmesg filtered for container references
        return read_dmesg(lines, r"(docker|containerd)") or                [f"[No docker logs found — journal and socket both unavailable on {NODE_NAME}]"]

    # CasaOS: file logs + journal
    if log_type == "casaos":
        result = read_casaos_logs(lines)
        if result:
            return result
        result = read_journal(lines, src.get("journal_args", []))
        if result:
            return result
        return ["[No CasaOS logs found]"]

    # For journald-only systems: try journal FIRST before log files
    if src.get("journal_first"):
        # dmesg is best for kernel/boot/disk
        if log_type in ("kernel", "boot", "disk"):
            dmesg = read_dmesg(lines, pattern if log_type == "disk" else None)
            if dmesg:
                return dmesg
        result = read_journal(lines, src.get("journal_args", []), pattern)
        if result:
            return result

    # Try flat log files (traditional syslog distros)
    for fname in src.get("files", []):
        chunk = read_log_tail(fname, lines, pattern)
        if chunk:
            return chunk

    # Final fallback: journal without journal_first flag
    if not src.get("journal_first"):
        result = read_journal(lines, src.get("journal_args", []), pattern)
        if result:
            return result

    return [f"[No '{log_type}' logs found on {NODE_NAME}]"]

# ── Sources endpoint ─────────────────────────────────────────────────────────

@app.get("/api/sources")
async def get_sources():
    result = {}
    always_available = ("disk", "boot", "docker", "casaos", "syslog", "kernel", "auth")
    for key, src in LOG_SOURCES.items():
        exists = any((LOG_ROOT / f).exists() for f in src.get("files", []))
        casaos_exists = Path(src.get("casaos_log_dir", "")).exists() if "casaos_log_dir" in src else False
        result[key] = {
            "label": src["label"], "desc": src["desc"],
            "available": exists or casaos_exists or key in always_available,
        }
    result["smart"] = {
        "label": "S.M.A.R.T", "desc": "Drive health",
        "available": True,
    }
    return result

# ── Logs endpoint ────────────────────────────────────────────────────────────

@app.get("/api/logs/{log_type}")
async def get_logs(log_type: str, lines: int = Query(default=100, ge=10, le=2000)):
    if log_type == "smart":
        return get_smart_data()
    if log_type not in LOG_SOURCES:
        raise HTTPException(404, f"Unknown log type: {log_type}")
    src     = LOG_SOURCES[log_type]
    entries = read_log_source(log_type, lines)
    return {
        "type": log_type, "label": src["label"],
        "lines": len(entries), "requested": lines,
        "entries": entries, "fetched_at": datetime.now().isoformat(),
        "node": NODE_NAME,
    }

# ── S.M.A.R.T ────────────────────────────────────────────────────────────────

def _smartctl_run(drive: str, extra: list = None) -> subprocess.CompletedProcess:
    cmd = ["smartctl", "-H", "-A"] + (extra or []) + [drive]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, errors="replace")
    out = result.stdout + result.stderr
    # If SMART is disabled on the device, enable it and retry once
    if "SMART support is:     Disabled" in out or "Informational Exceptions (SMART) disabled" in out:
        enable_cmd = ["smartctl", "-s", "on"] + (extra or []) + [drive]
        subprocess.run(enable_cmd, capture_output=True, text=True, timeout=10, errors="replace")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, errors="replace")
    return result

def _parse_smart(drive: str, result: subprocess.CompletedProcess) -> dict:
    output = result.stdout + result.stderr
    health_line = next((l for l in output.splitlines() if "SMART overall-health" in l), "")
    attrs, in_table = [], False
    for line in output.splitlines():
        if "ID#" in line and "ATTRIBUTE_NAME" in line:
            in_table = True; continue
        if in_table and line.strip():
            parts = line.split()
            if len(parts) >= 10:
                name, raw = parts[1], parts[-1]
                if any(k in name for k in ["Reallocated","Pending","Uncorrectable",
                                            "Temperature","Power_On","Wear",
                                            "Erase_Fail","Program_Fail"]):
                    attrs.append(f"  {name}: {raw}")
    return {"drive": drive, "health": health_line.strip() or "Unknown", "attrs": attrs[:12], "raw": output[:3000]}

# Transport retry order for USB/SAT bridges — ordered from most to least common
_SAT_TRANSPORTS = [
    ["-d", "scsi"],         # SCSI-mode USB enclosures (Seagate Expansion, WD Elements, etc.)
    ["-d", "sat"],          # most common USB-SATA bridges (UAS)
    ["-d", "sat,16"],       # bridges that need 16-byte ATA pass-through
    ["-d", "sat,12"],       # bridges that need 12-byte ATA pass-through
    ["-d", "auto"],         # let smartctl guess
    ["-d", "usb"],          # generic USB mass-storage
    ["-d", "usbjmicron"],   # JMicron-based USB bridges
    ["-d", "usbsunplus"],   # SunPlus USB bridges
    ["-d", "usbprolific"],  # Prolific USB bridges
    ["-d", "sat", "-T", "permissive"],  # permissive mode for picky bridges
]
# Devices that never have real SMART data
_VIRTUAL_DEVS = re.compile(r"(zram|loop|ram|dm-)")


_REAL_MOUNT = re.compile(r"^(/$|/boot(/firmware)?|/mnt/|/media/|/home/|/data/|/storage/)")
# Maps partition to parent disk: sda1→sda, mmcblk0p2→mmcblk0, nvme0n1p1→nvme0n1
_PART_RE    = re.compile(r"^(/dev/(?:sd[a-z]+|hd[a-z]+|vd[a-z]+|nvme\d+n\d+|mmcblk\d+))(?:p\d+|\d+)$")

def _get_mount_map() -> dict:
    """Return {'/dev/sda': '/mnt/Hannibal', ...} by reading the HOST mount table.
    With pid:host, /proc/1/mounts is the host init's mount namespace — has all
    real disk mounts. /proc/mounts is the container's namespace (only bind mounts).
    """
    candidates: dict = {}

    for mounts_file in ["/proc/1/mounts", "/proc/mounts"]:
        try:
            with open(mounts_file) as f:
                lines = f.readlines()
            if not lines:
                continue
            for line in lines:
                parts = line.split()
                if len(parts) < 2:
                    continue
                dev, mp = parts[0], parts[1]
                if not dev.startswith("/dev/"):
                    continue
                if not _REAL_MOUNT.match(mp):
                    continue
                # Map partition → parent disk
                disk = dev
                m = _PART_RE.match(dev)
                if m:
                    disk = m.group(1)
                candidates.setdefault(disk, []).append(mp)
            if candidates:
                break  # got data from first file, don't try second
        except Exception as e:
            _app_log(f"mount map {mounts_file} error: {e}", "WARN")

    # Pick best mount per disk: /mnt/* > /media/* > /boot* > /
    result = {}
    for disk, mps in candidates.items():
        for prefix in ("/mnt/", "/media/", "/", "/boot"):
            match = next((mp for mp in mps if mp.startswith(prefix)), None)
            if match:
                result[disk] = match
                break
    return result


def get_smart_data() -> dict:
    # Enumerate all block devices
    drives = []
    lsblk = _run(["lsblk", "-dno", "NAME,TYPE"])
    for line in lsblk.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "disk":
            drives.append(f"/dev/{parts[0]}")
    if not drives:
        drives = ["/dev/sda", "/dev/mmcblk0"]

    mount_map = _get_mount_map()
    smart_results, unreadable = [], []

    for drive in drives:
        mount = mount_map.get(drive, "")

        # Skip obvious virtual devices immediately
        if _VIRTUAL_DEVS.search(drive):
            unreadable.append({
                "drive": drive, "mount": mount,
                "health": "Virtual device — no SMART",
                "attrs": [], "raw": "",
            })
            continue

        try:
            result = _smartctl_run(drive)
            output = result.stdout + result.stderr

            # Round 1: retry disk device with SAT transport variants
            used_transport = None
            if "SMART overall-health" not in output and "SMART Health Status" not in output:
                for flags in _SAT_TRANSPORTS:
                    retry = _smartctl_run(drive, flags)
                    retry_out = retry.stdout + retry.stderr
                    if "SMART overall-health" in retry_out or "SMART Health Status" in retry_out:
                        result = retry
                        output = retry_out
                        used_transport = " ".join(flags)
                        _app_log(f"SMART: {drive} readable via {used_transport}")
                        break

            # Round 2: try first partition (e.g. sda1) — some USB bridges only respond
            # to partition nodes, not the disk device (sda fails, sda1 works)
            used_partition = None
            if "SMART overall-health" not in output and "SMART Health Status" not in output:
                part_suffix = "p1" if "mmcblk" in drive else "1"
                part_dev = drive + part_suffix
                part_result = _smartctl_run(part_dev)
                part_out = part_result.stdout + part_result.stderr
                if "SMART Health Status" in part_out or "SMART overall-health" in part_out:
                    result = part_result
                    output = part_out
                    used_partition = part_dev
                    _app_log(f"SMART: {drive} readable via partition {part_dev}")

            entry = _parse_smart(drive, result)
            entry["mount"] = mount
            if used_transport:
                entry["transport"] = used_transport
            if used_partition:
                entry["via_partition"] = used_partition

            # Normalise both ATA and SCSI health strings
            has_health = "SMART overall-health" in output or "SMART Health Status" in output

            def _extract_health(out: str) -> str:
                # ATA: "SMART overall-health self-assessment test result: PASSED"
                for line in out.splitlines():
                    if "SMART overall-health" in line:
                        return line.replace(
                            "SMART overall-health self-assessment test result: ", ""
                        ).strip()
                # SCSI: "SMART Health Status: OK"
                for line in out.splitlines():
                    if "SMART Health Status" in line:
                        val = line.split(":", 1)[-1].strip()
                        # Normalise "OK" → "PASSED" for UI consistency
                        return "PASSED" if val == "OK" else val
                return "Unknown"

            if has_health:
                entry["health"] = _extract_health(output)
                smart_results.append(entry)
            elif "mmcblk" in drive:
                entry["health"] = "SD Card — SMART not supported"
                unreadable.append(entry)
            else:
                label = f"USB bridge — SMART unavailable ({mount})" if mount else "USB bridge — SMART unavailable"
                entry["health"] = label
                unreadable.append(entry)
                _app_log(f"SMART: {drive} ({mount or 'unmounted'}) — no transport worked", "WARN")

        except FileNotFoundError:
            unreadable.append({"drive": drive, "mount": mount, "health": "smartctl not installed", "attrs": [], "raw": ""})
        except Exception as e:
            unreadable.append({"drive": drive, "mount": mount, "health": f"Error: {e}", "attrs": [], "raw": str(e)})

    return {"drives": smart_results, "unreadable": unreadable}

# ── AI Analyze ───────────────────────────────────────────────────────────────

def _build_prompt(log_type: str, log_lines: list) -> str:
    src = LOG_SOURCES.get(log_type, {"label": log_type.upper(), "desc": log_type})
    sample = "\n".join(log_lines[-80:])
    return (
        f"You are a senior Linux sysadmin AI analyzing {src['label']} logs "
        f"from a Raspberry Pi node '{NODE_NAME}'.\n\n"
        f"Log source: {src['desc']}\n"
        f"Lines analyzed: {len(log_lines)}\n"
        f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"LOG SAMPLE:\n---\n{sample}\n---\n\n"
        "Respond in EXACTLY this format, nothing else:\n\n"
        "🔍 SUMMARY: <one sentence — what is happening in these logs>\n"
        "⚠️  ISSUES: <specific errors or anomalies found — or \"None detected\">\n"
        "🔧 ACTION: <one concrete command or fix — or \"No action needed\">\n"
        "📊 SEVERITY: <NORMAL | WARNING | CRITICAL> — <one-sentence reason>"
    )

@app.get("/api/analyze/{log_type}")
async def analyze_logs(log_type: str, lines: int = Query(default=100, ge=10, le=500)):
    if not OLLAMA_HOST:
        async def no_ollama():
            yield "data: [Ollama not configured — set OLLAMA_HOST env var]\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(no_ollama(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    if log_type == "smart":
        smart = get_smart_data()
        log_lines = []
        for d in smart["drives"]:
            log_lines.append(f"=== {d['drive']} — {d['health']} ===")
            log_lines.extend(d["attrs"])
        for d in smart["unreadable"]:
            log_lines.append(f"=== {d['drive']} — {d['health']} (unreadable) ===")
    elif log_type in LOG_SOURCES:
        log_lines = read_log_source(log_type, lines)
    else:
        raise HTTPException(404, f"Unknown log type: {log_type}")

    if not log_lines:
        log_lines = ["[No log data available]"]

    prompt = _build_prompt(log_type, log_lines)

    async def stream():
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                async with client.stream("POST", f"{OLLAMA_HOST}/api/generate",
                    json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": True,
                          "options": {"temperature": 0.1, "num_predict": 350}}) as resp:
                    if resp.status_code != 200:
                        yield f"data: [Ollama error {resp.status_code}]\n\n"
                        return
                    async for line in resp.aiter_lines():
                        if line:
                            try:
                                j = json.loads(line)
                                if j.get("response"):
                                    yield f"data: {j['response']}\n\n"
                                if j.get("done"):
                                    break
                            except Exception:
                                pass
        except Exception as e:
            yield f"data: [Connection error: {e}]\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


_PI_DASHBOARD_HTML = r"""<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,interactive-widget=resizes-content"/>

<title>AXIOM · Pi Node</title>
<meta name="description" content="AXIOM Pi Node — real-time log viewer with AI analysis."/>
<meta name="application-name" content="AXIOM Pi"/>
<meta name="robots" content="noindex,nofollow"/>

<!-- Theme / color -->
<meta name="theme-color" content="#f59e0b"/>
<meta name="color-scheme" content="dark"/>
<meta name="msapplication-TileColor" content="#0c0a08"/>
<meta name="msapplication-TileImage" content="data:image/png;base64,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"/>
<meta name="msapplication-config" content="none"/>

<!-- iOS / iPadOS PWA -->
<meta name="apple-mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"/>
<meta name="apple-mobile-web-app-title" content="AXIOM Pi"/>
<meta name="format-detection" content="telephone=no"/>

<!-- Apple touch icons — all sizes (inline data URIs, no external files needed) -->
<link rel="apple-touch-icon"              href="data:image/png;base64,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"/>
<link rel="apple-touch-icon" sizes="57x57"   href="data:image/png;base64,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"/>
<link rel="apple-touch-icon" sizes="60x60"   href="data:image/png;base64,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"/>
<link rel="apple-touch-icon" sizes="72x72"   href="data:image/png;base64,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"/>
<link rel="apple-touch-icon" sizes="76x76"   href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEwAAABMCAYAAADHl1ErAAAABmJLR0QA/wD/AP+gvaeTAAAIIklEQVR4nO2cf2yV1RnHP8977+392R8WxMJEGBTqRiZUApmoZV0URSgwp8aNjRiYLNmiRjLmXIJhsmQOBplui1sMEHG6zGmcUCxskoVCJusG4kRZhYxqscWIbWl7f/X2vmd/3JbbQnt/9J773kvXT3L/Oe95z3nO973nvOec93mOkIRip3N6r91YIUotAiajuBahMNl9VwghBecEdQxlvNwdDO4GgolukOEueL3eOaKiTwF36LYyf5ELoLZ2B0KbgfCQOYZIsxV6nVuUkkcAI6v25SuKRgNZ1RkMNlx6aZBgpaUU9YRcL/N/9a8aGgV+hbovEAi/MTB9oGB2n8dVy5hYA4kI6utdgfCe/oSLXa7Q69xMnohV5I798gCHwnih0Omc2Z8gcHGAP0qOx6wSDzx0u8F3viIYAn/8h+KpWpPzXbm0CkDe6Q4E5wJRG4DTYXseKM+VOe4C+G61sH2NjarrBbsNbAbMvk741gIDU8GJs9AbzZWFlDkd9uaeSO8xKXI6y02bnMqFFTYD7p0vPLbUYFJJPP3MpwqAz18dH2JbOuDntSZ/alBETastjZnQHQhNtdldBasFFllde1WFsONBg1W3GBS6YmntfvhFnclDuxQ76hUtHTBniuB1QqELFt8gLLtRON8Nja1WW0yh3WE7Ij6Paz8WClY5BZ5YYbBgRvzfEwjDjnrFL/crukJqUH6PE1ZXCY/eIfhc8XuOnoEnXzc5cnpw/myiYKf4vK5GFDOTZ8+M6dcIP1oi1FQK0tfuXhP+8JZiy16TTzoT31/qhe/fZvBgteC0x9PrGxUbXlH8p9UK4dRx8bldndlcG/Y3dG21UKChoZkKnyHt4vO4svJohutK/zoDmzR0pcopsGGFwc0pdm1daBfMYYP7vyz8cInBhKJ4+qlPFJv3KnYf09uQqgph493CrM/FhWv3w6/fNHnub4pwr9bq9AkmAjWVwo9r5LLpwLY6k5feyt50wBBYWilsWG5w3bh4enMbPL3f5MW/K0xNz0mLYPOmwRPLDeZPjwvlDyuePRB70sGeTGtIjQI7PHCrwQ8WC8WeePrxj2DTn00Of5C5ahkJNrNMWH9XbG7UT09v7pc0xR54+HaDNQsFd0E8vb5R8eRrJu+eHXnZIxJsUgmsW2zwzZsEW9/q01RQ+7bip7tNPjw/coN0kg070xJs4OLY5Yin1zcqfvKayYkMnlw20dkTUhKsf2xYfxcUueOV6hwbrEDHWJtUsFsrhGe+ffni+Gd7FLvfVqg81Ooaj4e7y8spLy4hFI1y5FwrdU1N9JomIrCsUnh8iLf5wy+YHGpM3KCkgtWuM5g3LVbw+S7Ytk+x67BJJHdbLQm5b8ZMtlYtxG23D0p/v+0zVu6ro7kr1v8cNlh1i8G6O4Xxfeucf/5XsXRb4rlP0g1DpyMmVtOnivkbTbYfzF+x5pWV8avqr14mFsAXS8fx+zsXYzdiTY5EYftBk/kbTZr6tpP625qIlHdYO0OCP5yH/W8A3/vSbGwyfKNnlY6j+trJg9L8YUVnKLlQ/Yyqz2iVEyZoyZOIUSWYw0jenIIU8iRiVAn2QUd78jztyfMkYlQJ9uLJkwmvnw8GqWtqyqiOUSXYq6dPsfO9E0Ne80cirD3wJl2RzHYCLn//XsEoYP3hQ/y1uZmVFdcz46oSgr1RjrS28Nt3/31xDpYJ2gSbVAKb7jGYXJr6Kzp7NAPNdAPNbYrfHDBp0bRzok2wLd8wuG1WPog1mNnXCS6Hwcpn9exeahvDpozXVZJ+dNqmTbDtB8nLhbhSMdt0oa1L7qw3OXJamFGmq0Q9nDoHJ1v0PUmtb8mTLYqTLTpLzD9G1TzMCsYES5MxwdJE6xg2sYRBX7ut4mwbfNZtTV3aBFtQDl+bJ8M7/mcRU8Hzh+C9j7M/r9HWJW+uyI1YEHMVuGmGRXXpKqjNoi4xbP1+a+rR1iVfaVAsnyuUenWVmDqtHVD3jjXLDG2CXQjCrsN5uDbSzNi0Ik3GBEuTMcHSZEywNNE26LscUDERXAXJ82YT04zN/Fs7slO+NsG+MAnGF+bHFnWxBy4EFYEhY2ozI+UuWeRSeJ3DC+JOwZHDKgRwO5JmA8DrFIrScMJMKlg4Eits6tVCw0aDNQsNHLbL833Unj9zsO6woj3JzN9hgzULDRo2Gkzt8xPrb2sitDrU+ZyCNztxEikTNWPLtOHczLPuUAfWuHNbgSUumwPJpjt3NrHcKfhSxtzOR0i+BjZk0z1+VIbOZNM9Xntw1uNLhWkT4sa2dsDWseCs4RkL/xsh2Y7VTjd2XBfi87jagKuyUjr6Y7VzG8KsOsTncR8HNTub1YC+IHldseMj5H3xelw7BR6wojZIP1Y727Hj6SCwT4o8ziUmUmtZrX0kG6xNZe3LIxWUkkcFKPB5XE3ARKsNuMKOksFmEgtTK/S41ir4XW7MiB1WtLpKeGSRMWhxD3AhAE//xWRHvbJsAjwUCqn1B4I1/Y/Q7vO4j4K6IXcmDV7c24zcL7EGEMUw53R395y4+J8vdDpnKpvRAKo4l5ZB/LC1zoTnXVqHUmqzPxh+DC45A7HQ46xRyKtAihu8ox+Buq5AqAaIwiVb1F2B8B4TtUKBRa4d+Y1AncMVup8+sWCIPf1AIPyGwqgGEkc6jW6iSqnNXYFQTVsbg6bUQ3zOgEgk0tIT6X2uwGHvAXUjiMsaO3OPQvaKYd7jD/S8RCx8aRCpfBtz+dzuZYh5r0LmCpQB+XEGZuZ0Ah8LfGgq2W9X6vULodCZRDf8D4de0kc2tqDiAAAAAElFTkSuQmCC"/>
<link rel="apple-touch-icon" sizes="114x114" href="data:image/png;base64,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"/>
<link rel="apple-touch-icon" sizes="120x120" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAHgAAAB4CAYAAAA5ZDbSAAAABmJLR0QA/wD/AP+gvaeTAAALz0lEQVR4nO2deZAU1R3HP6/n7plldwkgEA7B5ZBICWIkGkVBQeXyIkUsrxKwYqlRqyxLKoqVIqmUR1kaNaWmFlJGo6YSBQLIAhawavBAjoiiCAjIIS66C7s7M7s7M/3yx+zgLswsc/R09/T257+dfr396/nO6/d7v9/r9xPkSSgU6kMiMRHBJUh5NoLhIEMgKvL9nw5I4BiCo0i5A8QWDeXtSCSypf1Yzogc2yuhQOB6IeRcCZMBVz4XdcgNCfsUIV4QLk91Y2NjfS7nZi1wKBCYjSIXIhmeu4kO+iCOC8FjTeHoM0BLVmecrkEgEBjkEnIRcEWh5jnoxhcaym2RSGTT6Rp2+YhVVd9Ul6AGOEc30xz0oLdA3u71uH5oiyW6FDmjwGWqf45A/AMI6W6egx4oIKZ5vZ6ebbH46kyN0gpcpvrnSKjOdNzBUoz3etx922LxlekOniKgqvqmtvdcR9zS4Xyvx+1ui8XXnXygk4hJh4oanMdyKTLB4/XsisXi2zt+2MmLDqn+tdjUW540SjDvsuTtVm+QrNuRV9zA2kiaXJoce7y1dU/qoxMChwKB2Qj5hjmWFY8xg2DBtQoXD+88I9z0tWThMsnHe2wmtKC2OdwykfbIV+quXaGgf4edghhn9hbMnya4dpxAZJjtSwlLN0seWynZd9Q+QkshbgqHo69Bu8ChQGAWQv7LXLP0oTII91yhcMdEgc+d3TmxBLzxoeSJlRp1jcW1zyD2NEdaRgJxF4DP634KqDLXpsJQffDbyYJF85KPY7dyaps9dZKGMPQMdu7SLgXOHSS47WKBS4H/HUiKXsL09Hjdu2Kx+KciFAr1QYsfpkSnRW4FbrxQ8OA0hTN6pG9TH4a/vKPx1/USTcutfVu8eLYXFcF7zeGWCaKUnasJIwR/mCUY2S/9IBtphcXvSp5eLWlu6TzGql6Yc6ng/isFZf705+/5Ljk+L98qkaU3REu3xlARUgPPg7zbbGtyYdwQePQahV9UpRcmlzE1mzF7635YuFRj465SU1neJUIB/zoEE802JRuqzhA8NE0w87z0wkoJy7dK/rRcsjdHr3hAT7j/SoWbLhIoGbzud3dKfv+W5PNDJSK04HURUv3fAAPNtqUr+lXAA1cr3HhheucJ4OM9koXLNDZ9Xdi1zh0EC65RuGREepU1CSu2Jq914IfCrmUAn4lQwN+IoMxsS9IR9Anuulxw9xWCgDd9m6+OSJ58W/KfLfr2qgkjBI9epzB6QPrjbXF4+X2NJ1ZKGqO6XlpP6kVI9SeADP3CHLxumD1eMH+6Qq8MP71DDfB0jcZrH0gSWnHsEAJmjBU8MlNhcK/0bY5F4Lm1GtUbJC2x4thRAAkRUv2WGVCs+oVa5QeXD5YRuBQeiWYOGfliusCZkgEprOjUZOP0WSWZYZrAdpiWFHPapheGC2zHwIKegRe9MUzgbhAaZMIIwcIbBGf3zz10WiyKLnCuyYCSDe63Y7X7LarAhSQDSh2rPLGKIrCVxySjMdvn0FXgUvAqzcKsWYMuAhuZDCh1jE5mFCRwKUZ2rIJRkbu8BPa6Ye6lgvumKFQG07c5UA+Pr9B4c5NE64baVvh8DAiV4XO7ONjUxHeRyCltFAE3/Fzw0HSFgT3T/5+GMPx5jcai2vw87rwEXjRPYfqY9I+YlEGLayWtJT7lyYfLBw7ivjFjGd+vH64O63W/bKhn8eef88oXO4hpnbMRPnfS4+6qw6zYJplbnXsWI2eBA17Y+5TrlLXG0TaorpU8u0azcn60aHhdLp66ZAI3jhjZZbstdXXcsnpV2h7dIwD3TlGYd+mpQ56UMOSBBNG23OzKWeAeAdj1ZOcFmP/8SPLYco3Dx3K7uJ14YdLl/GpYdu8NfHWsgauWLKGxrTXt8f4VMH+GwuzxnXvRsAcTOXceXRL9j/y7e4t7XVVV1uICDK+o5OELLsh4/PCx5HeqB5ZayVGqPHDeuJzPufXsUfRR1SJY0xlH4AIZWl7OyMoMLnAXeBSFyYMGF8GizjgCF8jwPMRNMayyUkdL0uMIXCCqO/83fkLuLN+OKwBH4AI5kma6k/W54bCOlqTHEbhAttbVEYnlt7zzvW8P62zNqTgCF0g0HufN3btyPm9nQz2fHDlSBIs64wisA49v/oT6lqx2FgRAk5KHN24kYcC6JEdgHTgSDnPLmhqaYqePI0pgwQcb2XDwQPENwxFYNz769luuWrKETV08dg80NXFzzSpe2v6pYXYV30/vRuxsqOfqZUu4sF8/pgw+k6ryctyKwqHmZmoPHWTN/v20JozdG8IUgT2uZD558jmZF6SVNnVAHZqEhrBk95eSmv3SlH0/TBH4pdsVpmXIJ9sPwaRRgguGSubkkc8tFMPH4HFD6Ebi/si0MYJxQ4y/ruECV/XpfuKmMOPeDRd4d103XKDVjhn3brjAm/fCym3dT+SV2ySb9xp/XVOcrN/8TbO5F50k5UXXfilZVGvOj9oUgWMJeHGd5MV13a8nG40TybI5jsA2xxHY5jgC2xxHYJvjCGxzHIFtjinzYJcCvxwmGPVTid9TWoGOuAbNLfDJXslnB8225vSYIvDNFwlGD4Tcyxdbh3MGCJZuhve/snawxvBH9KBetItb+lw5GgpY924IhgvcJ8PeUaVIwAtlfrOt6BrDBbbTtknRNmjKfrWsKRgu8Dffw3ZjVowWndXbIW7x+kqmOFmvbpSOF20Qpgic0JKbfr27E9prKDoUCSfQYXMcgW2OI7DNcQS2OY7ANscR2OY4AtscU+bBQsDAn0DvMusH67MlkYBwq2Df9zLn/SSLiSkCnzsQevcorQhWNlQGoW8PwYdfW0dkwx/R5QF7ipvC7YYhvaxzf4YLHLR4ek0PVJ91wq+6CPzHWQr9K7JrG7Z4ek0PIq2F9eD+FcnvVA9yHoNjieTm1B03BJ89XjBzrCurDcGPR+Foo7TtYzoeh73f59eDT7cheD5bQJiypb/jRXfGUlv6g1OUQy8sW5QjhVNWJ38sXVbnZEqpYLLZGF0Q2yltZxAlXdruZJzilD9iq+KUJ2PFgslGYevysh2xWsHkYmO1+3VKvOuIFQtiGyZwCrPHpGJgZZ/DcIFTmOVV6kkpzBpMEziF0QWT9aCUCmKbLnAKoyI7hVCKkTvLCAzJJMSMsYJHZioM7pW+zbEIPLdWo3qDpCW/ajY543UnM2bzpyv0Kkvf5lADPF2j8doHkoTx20JnRIRUfxywVE7HKl+oVX9wOZAQIdXXACLLdL2xmPlILIUh4/SI4yKk+j8Dfma2KV1hZDLD6GRAMZGwT4SC/teR/NpsY7KhmNMSO0zb0vCOCKm+u0E8b7YluaBnYMGOgZcTCPGsKPf5zkq4xG6zbcmHQpIZ3SF0ihSzBEBI9f8XuMhkc/Ii1+C+plkrGVBEYorb21cABIP+W4XkZbMtKgTVB3dOFNwzWRD0ZeiR7UUxzspQ/STcKnl+reTF9ZJIa9FMNQSJWBGORGek7tQTVP07BZhQ2UdfshlTT8aOCxCEFNc3RaNLTvyUg0H/zULyiplG6cmZvQXzpwmuHSc6reHuiJSwdHNynN1nryVEO5ojLaMBreOti1DQvwHJBLOsKgaZkhlWSQYUg1TvhZN2A+3h81VpitiCIEOAsHSZNEow77Lk7VZvkKzbYaseewIBq5oiLVM7/N2ZYNB/k5C8aqxZDjpxNCHF2Gg0eij1wSlJhlgsvt3rcXvAXo/qbkBcCjkrEmnZ1vHDtFmktlh8vdfj7gucb4hpDoUipWBuONz61skHMqYJ22LxlV6vpxIYX1TTHAolnhS3JW0co8s8cFssXuP1uOpATDldWwdTOCqFnJWu56bI6iVdVVXHKWh/B0bpZppDQQhYFZfijo4OVYZ2WeMvCwbulZLfgSwv0D6H/NmBFAuao9GMvbYjOb9mX15eXqnF2+ZJKe8EhuZsnkM+xCRitSJZ3BSNLgOyXqRU0D4KqqqOVdCmgjwPKUYh6ANU4GywVggNAhol7ALxBZJaxeNZ39jYWJ/PP/s/mFAVC8+AooMAAAAASUVORK5CYII="/>
<link rel="apple-touch-icon" sizes="144x144" href="data:image/png;base64,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"/>
<link rel="apple-touch-icon" sizes="152x152" href="data:image/png;base64,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"/>
<link rel="apple-touch-icon" sizes="167x167" href="data:image/png;base64,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"/>
<link rel="apple-touch-icon" sizes="180x180" href="data:image/png;base64,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"/>

<!-- Android / Chrome PWA -->
<meta name="mobile-web-app-capable" content="yes"/>

<!-- Favicons -->
<link rel="icon" type="image/svg+xml"  href="data:image/svg+xml,%3Csvg%20xmlns=%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox=%220%200%2064%2064%22%3E%0A%20%20%3Crect%20width=%2264%22%20height=%2264%22%20rx=%2211%22%20fill=%22%230c0a08%22%2F%3E%0A%20%20%3Cpolygon%20points=%2232%2C6%2056%2C19%2056%2C45%2032%2C58%208%2C45%208%2C19%22%0A%20%20%20%20%20%20%20%20%20%20%20stroke=%22%23f59e0b%22%20stroke-width=%222.2%22%20fill=%22none%22%20stroke-linejoin=%22round%22%2F%3E%0A%20%20%3Cline%20x1=%2217%22%20y1=%2228%22%20x2=%2247%22%20y2=%2228%22%20stroke=%22%23f59e0b%22%20stroke-width=%223.5%22%20stroke-linecap=%22round%22%2F%3E%0A%20%20%3Cline%20x1=%2217%22%20y1=%2236%22%20x2=%2241%22%20y2=%2236%22%20stroke=%22%23f59e0b%22%20stroke-width=%223.5%22%20stroke-linecap=%22round%22%20opacity=%220.6%22%2F%3E%0A%20%20%3Cline%20x1=%2217%22%20y1=%2244%22%20x2=%2244%22%20y2=%2244%22%20stroke=%22%23f59e0b%22%20stroke-width=%223.5%22%20stroke-linecap=%22round%22%20opacity=%220.3%22%2F%3E%0A%20%20%3Ccircle%20cx=%2247%22%20cy=%2222%22%20r=%224%22%20fill=%22%23f87171%22%2F%3E%0A%3C%2Fsvg%3E"/>
<link rel="icon" type="image/png" sizes="16x16"   href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAABmJLR0QA/wD/AP+gvaeTAAABpUlEQVQ4jZWSz0tUYRSGn/ON8+vj4u2KqAtdmdPKRYsx0sDctMqN4KYRWrRwE7qwPyMokgIXMosgdwP+AVIg1MKFG4vEigJbODTpqI05eue0GNJ7u3dgepcv7/d+zzkccV3X8+uniwiTQCftaR9k5bh28iiRNLKMUADSwYRrYf6OYSwnbH1XTs9CBVkgn0omXXFsphr8OdUB0yPCcD+8WFPOfXgwbqgcK8V15aQe7NEDcWxGAYzA3evC+DWhuN5gazfMPNQrzIwJn/aUV+8Uv9H0DcBAFzydMfw4goWV6GOAL2XYfp/j7JfHk4JhoCtQMNgjlDaUtzvacmvD3d08uz3BZN8NShvKYI8A0BEM2TTcvyUkTLRAqFAqv+H55h6ed+nHROOlKJuHH9k5+BnyQwTnPnzYJZbgWwU+l6Mjtk3QSiECvwFfK4pIfNimoFYPewaaaFN54ebVFi8DGh0SpvJyMU7bh5TrEwqj0UMSx6b3Qa78Df7fKUtVHJtdBH34L6prYXaiueOl1w2qteg4orwUIOPY7GPQe4AXjcXqUJTVRPr33B+axJZkkxCX5gAAAABJRU5ErkJggg=="/>
<link rel="icon" type="image/png" sizes="32x32"   href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAABmJLR0QA/wD/AP+gvaeTAAADLklEQVRYheWXTUxcVRTHf+fOY2aYjwBKGqjYWpUBg4XYSNpG0lkYIBASqpkYE9xIu23UxEQSg21FF8TE2Jimi7ayMS5sSTDW8lFjrKluTKwlBgQ2M6DDosXhIzMM+t67XdCknZZheHyFxP/u5f3POf/7v+e9e65wDwVe7z5L0QE0A6WAi82FCUQ1XHJ70t2JBHMAAuD3expFy2UgsMlFV4SGqLJ048LS0rjcW/mw0+JKoOUFAeDKTY2tHaoQJvKT6QOuPLfRDRx2EhuuFC4cd9HwvBCuFF47qIjdgdgdRxIeN/OMtAR83imgbC0RFaVCZ6tQ+7Tw+TWbCz9qLBtePyS816IYjWu6+myGp9YoQTMmAZ/XJEfDlT0GbzcqXn0Ren6CM0M284uZHL9HePMIvNMo/DACH3+rid7OuS+mBHzerKxCH5yoV7SHhe//0HzYZzP1z+oZSwrg3WZFpFa4/Kum+4rN7YXs/BUF+NzQHhbeahB+n4STvZqRuLMue2aX0NEivFy17NqnA5rk0qM5MgQogUit8H6rIp6Arm9sfplw2t6ZOPAUdLYqKkqFzwY1X1y3Me37740HyScahLbDio6vLfpvrb+ozzC4WN9A3e4nuBH/m7azQ4SrTE6/4iLfLZwZvL8o9WBgkV+4Oqw3VBwgUh6ifs9e8g2D+j17iZSH6L8FV4c1RX7J4KosOTaEhzdttU3cEgG9E+MMxqKkTJPBWJTeifGsXCPrG+C53cL5Y4LbJavRVoAGhpi0NB/1a1Jmdg+2xAEnWNWB0bimrmtjn2Eu7GwHSgvhjZcEw4FM04Yvf9ZMz66Nv7MdmJ6FT777P/dAwCNUPwlqHTJnFmB0Ord7GakTSU1ztdBU47xgLjTVQHO1kEhmitqW4/iDo4pQycrH8c4aSB7Gdo1k/5GjGbdwKLUk4PeOoQnlYsLmj+UCMQn4vCeBU2sLWUa4Uug8qijIX36eW4SuPpvrfzq+Hp2T4mKC6ZT3N+BZJ6EbvprBvKVlvwAEPZ6Q7ZIBgX2O06wP81p0JJlcuuYC+NeyZoJBs8cyDY1QAhSw+b9pC/gL5CtLS1sqlb4JcBeIGlXQJpzNDwAAAABJRU5ErkJggg=="/>
<link rel="icon" type="image/png" sizes="48x48"   href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAYAAABXAvmHAAAABmJLR0QA/wD/AP+gvaeTAAAE60lEQVRogeWZa2wUVRTHf2e2j93ZbitbbBQopFV8ACZUbSFKmmhATHgrDXwgaEj4pInREAgk1EdNNRBfBD8pJD5i0BoDxoYQUCNBKcVYfOCDliLUUMtKpY99tJQ5fhhaobulO91p18j/0+6duef8/3PPPXPnHGEQfD7fRI/oWoEFCkVA/uB7xhh/ovwiwq6uSOw9IHrlRbnyd8DvW6+qlYA5phSTx0ks1nTHYgf7B/oFiN/07hR4PD28HKEXlVXd0WgNXBYQ8Ps2qOrLbnsqvBx8LefdtkyPoVLeGY3Wi2maEwysRlwMm0lBWL/AoKLMXuCaemVLrcUf7W55AJTfuqOxGZ7sTM8zwDw3bN5gwoaFBttXG8ycLHz+s3IqBItLhMfmGOSZwvdnlNhFF5wJ4zOzMpokYHrrFUpTsZWVAStmCRsXGeTnQMNpqNpt8XWjAnBvEVQuNZh1i9ARgW37LXZ8pUR7U9XAXskxve3AuJEYMAQWlgiblxhMzoeWdtjymUXNUUU1/v6H7hKeXyYUFwhnL8Crey0+OKxcskasoU1yTK/F1ek0KZTfLjz3iDB9otAehjcPWLz1pdLTd+15mR5YOVtYv8CgIBca25Qttcqn3yVQPDxUckyvo5klU2DzUoP7pwqRHth5UHltn9Idc0bAzIY15cLT84Ucr/DtKajaY1HX5MxO0gIKg/YGXV4q9Fmwq07ZWmvR1unIXxyCfnhirsHaB4TsDNj/k1L5idJ8LjkhwwpI1UGyGOkDGlJAoiV+YbfFkZPuEh+MRCH6+j6la4gQTShg8d1CdYXBjQH4tVWp2q0cOD56xM3MTBYVFRP0ZlPX2kpDKMTc6cLmpcIdNwuhLthUYyXc6AkFfLHRYEo+bKpRPj6aUpobFuN9PvYve5TCQAAABaqO1LHtWAMeA5aXCtUVwunz8OBL8USMREYNsXP6h0dGlzzAkzNLBsiDnc83lZaRm5XNJcvm0NJuc0rIdXTpDY/b8vLixjIMg+K83KTmp13AiY6OuLE+y6K5I7n8nHYB24810NLVNfBfgeqj9XT29iQ1P8Opw/umCuMDw9+XPGJUn/iIe4LFSF82u348S0MolPRsRwJWzxG2rhyNRbsENAJwvNeiIXn+6Q+hVOFoBd49pDS1WS6H0L/4qwu+aXT2wnS8B5w6GG1cXyEEUFwAAW/qji2F30My5CEtWTgSMPtW+2ziFjqi8OIeEn5+JovrK4TqmuBcp7oaQqk8fRjBHmg+l5rDq5F6Rru+QghgnN8uZLmJ7hiEkzu7xcERlUlBuHOCe1moH6pwuElHJCJhCFlqVwlWzBI8aQwyj2FzKAzanBLB8Uf9WIVQSh/14F7lzCmcVv7+v4UttxwMh1EvLQ6G08rZUHCr8pdSef3ZZQYzJsHfYdiexvJ6G1Awktn/gQZHSAKmd6/CwyM2QXyL6dgZu8V06IStorQYKpcYlLncYgLqxO/3rhblnZRNYTf5nppvsKZc8GYy8O6YO12IXbT3yxv7LC5E3PAGQKUAnhzT+wMwzS2rY9FmVQgrxlQByPX5yizRg0C2ey5GtdGNiqwLh6OvDGSfHJ+vAtH3gSz33bkLhR3hSGwtoANHte5otAaLecDJ9FG7NhTCKrKunzwkzv++gOldpcpKhGnATWPKMh4hhWaBWgvj7Ugk0nrlxX8A9DBcaWloMXEAAAAASUVORK5CYII="/>
<link rel="icon" type="image/png" sizes="96x96"   href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAGAAAABgCAYAAADimHc4AAAABmJLR0QA/wD/AP+gvaeTAAAKBUlEQVR4nO2dfXBU1RmHn/fuZr9DIAGLCWqo6USpH3wqARNaOwJSUdS2aqfVwWrt6DhotbUtUlR0tK212A5WOxaZtiNayxRFBWRoFYQoERD6oVHiRAVCRZKQbDab7O49/SNAks0m+5F79+6y+/y1e+/Ze989v3vOe+57z32PkDhS6HZPV6JmC0zXoVKgBBiRxDFOQlQrSBPCHlFsVpr9Jb/f/1miv5YEyni8bufNInI7cGbqhuYM3SheELv+YHt79/vxCg8pgM/tvhpRy4FxhpmXO4QRWeHv6PwZEBis0GACuLwe1wqBG82xLad4z64zrzUYbIy1c4AAxcWM6O5yvYRilumm5Q5NSiKXdnSE9kTviBbA5fO6NuQr3wzkqI5cEggE6vpu1fp+8XpcK/KVbxaqSENf4/P5Tum79UQLOOZw/5Z+w1JjbBHcWNNz/azconPoqMUGJYrin/7O4GwgDGA7ttnjcNhfIQvG9F6ncMvFwtPf06iuFKZXCDdUa4xwCe9+ougKW21hHITxBXZbcygcebvnK+B1OxeJyHJrLRuaAhtcO1245zKNMYWxy7QG4HebdJ5+XREMpde+JGnR7I6Ktra2ZhsgTkfBX4Biq62KhSZw5VRh1fdtfPMCwevs3RfWQVc9ZQBcBTDrLOHqaRpH/FDfBMoas+Ph1iMRFQqHN0uh212lRG232qJY1FQKSxZonHfawH1b6hVL1/R0OT+5TJg/SZCoMV19k+LR9YqXdmWkDIf9gWCp+Dyu+4ClVlvTl8pThSVXCJecM/A+cVcjPLBWp3Zf/0qdXA5LrtCY8aWBv9lSr1i2VmfvpyYZnCJK1Gwp9LjWK5hrtTEA44rhjjka364SbFr/fQ2fKR55WbFut0INcUHXVAr3XyVMKOsvhFKwbrfioXWKxsMZ0yKeEq/H9ZHAeCutGOmB2y/RuOkrgqug/75DR+HRV3VW1yrCemLH0wQumyT8fIHGaVGeLRSB595S/OJlncPtxtifOmq3+DyudsBnxek9DrhxlrBotjDC3f+K7ehSPLMFHtug6OhK7Yo1+/jDR7WKz+OKEHVHbDaawDemCYuv0Bhb1H+fGVfoUC3siB+Wb1SsfENPuIUZiC4+jyut8lvZR5eNgjvnDs/HGE3aBMikUUrlqcLdlwqXT449ylr2os72D9OjgukCnHmKDDlOX/aiYtO/remDE7nP+O9Bc20zTYCxRXD3PI3rqgR7VHPf3wzLN+o8W6uIpL/f7YcIzJ8kLJ4vlI/pf4XoCl7erXhgrc6nzSad32gBvE5hYQ38cK7gdfb/Q5kcqxkq1hTohpVvKB5/Taet09jzGiaAVX/AaNJ9AQ1bgHhNeE2d4sEXsyhef4wSH9w5V1hYow3oQg+0wG82GNOFDkuATHBiZhNvEDHcYF9KAqQSLMt2zBpGJyWAEcGybMfoG8mEBaipFP78Ay1msOxXr+o8l0SwLFP5cnEJt02cSHVpGaPdbj7v7GTLgf08sWcP/2k+cqKcXYNrq4QfzRsYSgmG4LtP6mypT0wEm6PAfl8iBe+apzHx9F7V24OKX69X3LpKZ2djj8PNZm459zxWzZnLuSWjKXQ4sIlQ6HBwTslorp8wgbbubnZ+9j+g57/u/RT+9KYi0A0TzwCnvadu7Laep3Dr9yZWIQkH4QpsvZ93NcIFS3Ue39hjQLZzVUUFD82YiS3ayx7DJsJDM2ZyZUVFv+2Bbnh8o+KCpTq7Gnu3962reKQUBW38XNHckcovMw+nzcayqplxywmwbPoMnLaBtdvc0VMnqZDWMHQmUl1axhc8noTKjvV6uai0zNDz57wAZ40aZWr5eOS8AMl2HLrBE11yXoD6lpbkyjcnVz4eOS/A1oMHONSR2IjiUEcH25oOGnr+nBegKxJhyVvb43YsCri3dhtdkYih5895AQD+vm8fi7dvIzJIDCWiFIu3b2NtQ4Ph57YbfsQs5Q//2subBw5w6/nnU11axhi3m8OdnWw9eIAV777Ley3mPBIzVQCbBtdcKEw6Y2AoNzNpIcjrbNJB+WH3x4rn3zb3sampAvzyGo3vzMyKmo/J9RcJU8oVd602TwHTfIDPJVxXlb2Vf5zrqgSfy7z/kXfCFmOaAP6gYnVtlseogdW1Cn/QvP9hqg/48fM6OxuzyQn3olSvEzYTUwWI6PBsreLZk6AlmEXeB1hMXgCLyQtgMXkBLCYvgMWYOgrSBKZ+EU4vzrwx6CfNinc+sn46jakCXD1VuLAifjkrmI5wRjG8UGetAqZ1Qa4CmJbhGeamncmAmX7pJu8DLMY0AYIhqDP+AZKh1DVg+Zs6pvqANe8oPm7ObCdsNaYKoCvY0QA7GvKxoMHI+wCLyQtgMXkBLCYvgMXkBbAYU0dBIlA6EorcmTcMjSYcgf2tikBXes9rqgBnnyqUZWQuxticViLUNqRXBNO6ILsNSo19l8F0NA3GjUpva01JgPLRQrHXaFOyl2JvT52kQsIChPrMyp5cDjvu11g0R/A4YpcPR+Cgse8ymI6uw/6WxO/aPQ5YNEfYcb/G5PLe7aEkZrCb+qL2yeqEjXxRO6lUBZmYcy3dWJaqoC+ZlHMtXWREso5o8ulqhp/zzvSETWbnXDOLdOW8y6csiyLrUpZFk0/alxympa1MV8614WJ1zjvLE7daucBCJgwicjZ1cabkvMup5N2ZmPPOsvT16VxgwegFIgxEF5/H3QqqKH5Z48kv4ECb+DzO3SATrbIAjF9gId0LRAyDfTZHQcFkYIqVVgRDPY74hR0Kj1M4Z5ycWBvM44CLJwgLpgiH2+GDQ0Mfq6ZSeOZmjRuqNXyu3u3HfczCpxV/fTszkg0qpFa8XudsUbLRamP6kkqwL5NGWUlwrwB2n8fVBIy22ppojFjIzcoFIuKho007tpak6xER7rHaoFhoAgumCD+dr3F6Sf99x31C9J32J0fg4XU6a3cqy9+AGRThA39HsFIARo5kZLjb9SEZ2AqO47DDwmrhjrnaoM+jmztg+QadZ7YqujN9VVXUbf5A1xM2gGCQYIG9ICSSGSvqxSKiw85GWLUV2oKKqePBcSxdcKAbnvqH4qY/6mz/EMvjS/FQ0NgR6LoJCPftNe0+t+s1hK9aZVgyZO2CzoCgLm8PdK3r+dyHwsLCEj0Sekeg3BLLcgL5vT/QeeuJb9G7PR7PNA21yaq745MaxWZ/Z/DrwIm5FwNiQIFAoE5JeBbQlE7bTnoUm13e4JX0qXyI0QKOU+RyjY9ovAKcbbZtOcCT/kDwDqIqH2DQTPdd4XBrdyi80uGweUGmkp/KnjQKGjXU9f5A12NAzPlyCU1ZKyx0nEVYW6yEbwGDTEbMcwJFPaJ+6w90rQSCQxVNas6gz+cbI3p4vlJ8DeE8oAzIsjnQRiNHQX2ukPcFVaujvRYIBOoS/fX/AWJF6XVQIl+AAAAAAElFTkSuQmCC"/>
<link rel="icon" type="image/png" sizes="192x192" href="data:image/png;base64,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"/>
<link rel="mask-icon" href="data:image/svg+xml,%3Csvg%20xmlns=%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox=%220%200%2064%2064%22%3E%0A%20%20%3Crect%20width=%2264%22%20height=%2264%22%20rx=%2211%22%20fill=%22%230c0a08%22%2F%3E%0A%20%20%3Cpolygon%20points=%2232%2C6%2056%2C19%2056%2C45%2032%2C58%208%2C45%208%2C19%22%0A%20%20%20%20%20%20%20%20%20%20%20stroke=%22%23f59e0b%22%20stroke-width=%222.2%22%20fill=%22none%22%20stroke-linejoin=%22round%22%2F%3E%0A%20%20%3Cline%20x1=%2217%22%20y1=%2228%22%20x2=%2247%22%20y2=%2228%22%20stroke=%22%23f59e0b%22%20stroke-width=%223.5%22%20stroke-linecap=%22round%22%2F%3E%0A%20%20%3Cline%20x1=%2217%22%20y1=%2236%22%20x2=%2241%22%20y2=%2236%22%20stroke=%22%23f59e0b%22%20stroke-width=%223.5%22%20stroke-linecap=%22round%22%20opacity=%220.6%22%2F%3E%0A%20%20%3Cline%20x1=%2217%22%20y1=%2244%22%20x2=%2244%22%20y2=%2244%22%20stroke=%22%23f59e0b%22%20stroke-width=%223.5%22%20stroke-linecap=%22round%22%20opacity=%220.3%22%2F%3E%0A%20%20%3Ccircle%20cx=%2247%22%20cy=%2222%22%20r=%224%22%20fill=%22%23f87171%22%2F%3E%0A%3C%2Fsvg%3E" color="#f59e0b"/>
<link rel="shortcut icon" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAABmJLR0QA/wD/AP+gvaeTAAADLklEQVRYheWXTUxcVRTHf+fOY2aYjwBKGqjYWpUBg4XYSNpG0lkYIBASqpkYE9xIu23UxEQSg21FF8TE2Jimi7ayMS5sSTDW8lFjrKluTKwlBgQ2M6DDosXhIzMM+t67XdCknZZheHyFxP/u5f3POf/7v+e9e65wDwVe7z5L0QE0A6WAi82FCUQ1XHJ70t2JBHMAAuD3expFy2UgsMlFV4SGqLJ048LS0rjcW/mw0+JKoOUFAeDKTY2tHaoQJvKT6QOuPLfRDRx2EhuuFC4cd9HwvBCuFF47qIjdgdgdRxIeN/OMtAR83imgbC0RFaVCZ6tQ+7Tw+TWbCz9qLBtePyS816IYjWu6+myGp9YoQTMmAZ/XJEfDlT0GbzcqXn0Ren6CM0M284uZHL9HePMIvNMo/DACH3+rid7OuS+mBHzerKxCH5yoV7SHhe//0HzYZzP1z+oZSwrg3WZFpFa4/Kum+4rN7YXs/BUF+NzQHhbeahB+n4STvZqRuLMue2aX0NEivFy17NqnA5rk0qM5MgQogUit8H6rIp6Arm9sfplw2t6ZOPAUdLYqKkqFzwY1X1y3Me37740HyScahLbDio6vLfpvrb+ozzC4WN9A3e4nuBH/m7azQ4SrTE6/4iLfLZwZvL8o9WBgkV+4Oqw3VBwgUh6ifs9e8g2D+j17iZSH6L8FV4c1RX7J4KosOTaEhzdttU3cEgG9E+MMxqKkTJPBWJTeifGsXCPrG+C53cL5Y4LbJavRVoAGhpi0NB/1a1Jmdg+2xAEnWNWB0bimrmtjn2Eu7GwHSgvhjZcEw4FM04Yvf9ZMz66Nv7MdmJ6FT777P/dAwCNUPwlqHTJnFmB0Ord7GakTSU1ztdBU47xgLjTVQHO1kEhmitqW4/iDo4pQycrH8c4aSB7Gdo1k/5GjGbdwKLUk4PeOoQnlYsLmj+UCMQn4vCeBU2sLWUa4Uug8qijIX36eW4SuPpvrfzq+Hp2T4mKC6ZT3N+BZJ6EbvprBvKVlvwAEPZ6Q7ZIBgX2O06wP81p0JJlcuuYC+NeyZoJBs8cyDY1QAhSw+b9pC/gL5CtLS1sqlb4JcBeIGlXQJpzNDwAAAABJRU5ErkJggg=="/>

<!-- PWA Manifest (inline JS blob — works even with no external file server) -->
<script>
(function(){
  const IC192="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",IC512="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",IC192M="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",IC512M="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",ICSVG="PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2NCA2NCI+CiAgPHJlY3Qgd2lkdGg9IjY0IiBoZWlnaHQ9IjY0IiByeD0iMTEiIGZpbGw9IiMwYzBhMDgiLz4KICA8cG9seWdvbiBwb2ludHM9IjMyLDYgNTYsMTkgNTYsNDUgMzIsNTggOCw0NSA4LDE5IgogICAgICAgICAgIHN0cm9rZT0iI2Y1OWUwYiIgc3Ryb2tlLXdpZHRoPSIyLjIiIGZpbGw9Im5vbmUiIHN0cm9rZS1saW5lam9pbj0icm91bmQiLz4KICA8bGluZSB4MT0iMTciIHkxPSIyOCIgeDI9IjQ3IiB5Mj0iMjgiIHN0cm9rZT0iI2Y1OWUwYiIgc3Ryb2tlLXdpZHRoPSIzLjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPgogIDxsaW5lIHgxPSIxNyIgeTE9IjM2IiB4Mj0iNDEiIHkyPSIzNiIgc3Ryb2tlPSIjZjU5ZTBiIiBzdHJva2Utd2lkdGg9IjMuNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBvcGFjaXR5PSIwLjYiLz4KICA8bGluZSB4MT0iMTciIHkxPSI0NCIgeDI9IjQ0IiB5Mj0iNDQiIHN0cm9rZT0iI2Y1OWUwYiIgc3Ryb2tlLXdpZHRoPSIzLjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIgb3BhY2l0eT0iMC4zIi8+CiAgPGNpcmNsZSBjeD0iNDciIGN5PSIyMiIgcj0iNCIgZmlsbD0iI2Y4NzE3MSIvPgo8L3N2Zz4=";
  const manifest = {
    id: "/",
    name: "AXIOM Pi Node",
    short_name: "AXIOM Pi",
    description: "Real-time Raspberry Pi log viewer with AI-powered analysis.",
    start_url: "/",
    scope: "/",
    display: "standalone",
    display_override: ["window-controls-overlay","standalone","minimal-ui"],
    background_color: "#0c0a08",
    theme_color: "#f59e0b",
    orientation: "any",
    lang: "en",
    categories: ["productivity","utilities"],
    prefer_related_applications: false,
    icons: [
      { src:"data:image/png;base64,"+IC192,  type:"image/png", sizes:"192x192", purpose:"any" },
      { src:"data:image/png;base64,"+IC512,  type:"image/png", sizes:"512x512", purpose:"any" },
      { src:"data:image/png;base64,"+IC192M, type:"image/png", sizes:"192x192", purpose:"maskable" },
      { src:"data:image/png;base64,"+IC512M, type:"image/png", sizes:"512x512", purpose:"maskable" },
      { src:"data:image/svg+xml;base64,"+ICSVG, type:"image/svg+xml", sizes:"any", purpose:"any" }
    ],
    shortcuts: [
      { name:"System Logs", url:"/?src=syslog", icons:[{src:"data:image/png;base64,"+IC192,sizes:"192x192"}] },
      { name:"S.M.A.R.T",   url:"/?src=smart",  icons:[{src:"data:image/png;base64,"+IC192,sizes:"192x192"}] },
      { name:"Docker Logs", url:"/?src=docker", icons:[{src:"data:image/png;base64,"+IC192,sizes:"192x192"}] }
    ]
  };
  const blob = new Blob([JSON.stringify(manifest)], {type:"application/manifest+json"});
  const link = document.createElement("link");
  link.rel = "manifest"; link.href = URL.createObjectURL(blob);
  document.head.appendChild(link);
})();
</script>

<!-- Service Worker (inline blob) — caches shell for offline use -->
<script>
(function(){
  if(!('serviceWorker' in navigator)) return;
  const SW=`const VER='axiom-pi-v2';const SHELL=['/'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(VER).then(c=>c.addAll(SHELL).catch(()=>{})).then(()=>self.skipWaiting()));});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==VER).map(k=>caches.delete(k)))).then(()=>self.clients.claim()));});
self.addEventListener('fetch',e=>{
  const u=new URL(e.request.url);
  if(u.pathname.startsWith('/api/'))return;
  if(e.request.mode==='navigate'){e.respondWith(fetch(e.request).then(r=>{caches.open(VER).then(c=>c.put(e.request,r.clone()));return r;}).catch(()=>caches.match('/')));return;}
  e.respondWith(caches.open(VER).then(cache=>cache.match(e.request).then(cached=>{
    const net=fetch(e.request).then(r=>{if(r.ok)cache.put(e.request,r.clone());return r;}).catch(()=>cached);
    return cached||net;
  })));
});`;
  const swBlob=new Blob([SW],{type:'text/javascript'});
  window.addEventListener('load',()=>{
    navigator.serviceWorker.register(URL.createObjectURL(swBlob),{scope:'/'}).catch(()=>{});
  });
  window.addEventListener('appinstalled',()=>{
    window.__a2hs=null;
    const b=document.getElementById('pwa-install-banner');if(b)b.remove();
  });
})();
</script>

<!-- PWA Install Prompt: Android (beforeinstallprompt) + iOS Safari (manual) -->
<script>
(function(){
  var SVG_ICON='data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2NCA2NCI+CiAgPHJlY3Qgd2lkdGg9IjY0IiBoZWlnaHQ9IjY0IiByeD0iMTEiIGZpbGw9IiMwYzBhMDgiLz4KICA8cG9seWdvbiBwb2ludHM9IjMyLDYgNTYsMTkgNTYsNDUgMzIsNTggOCw0NSA4LDE5IgogICAgICAgICAgIHN0cm9rZT0iI2Y1OWUwYiIgc3Ryb2tlLXdpZHRoPSIyLjIiIGZpbGw9Im5vbmUiIHN0cm9rZS1saW5lam9pbj0icm91bmQiLz4KICA8bGluZSB4MT0iMTciIHkxPSIyOCIgeDI9IjQ3IiB5Mj0iMjgiIHN0cm9rZT0iI2Y1OWUwYiIgc3Ryb2tlLXdpZHRoPSIzLjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPgogIDxsaW5lIHgxPSIxNyIgeTE9IjM2IiB4Mj0iNDEiIHkyPSIzNiIgc3Ryb2tlPSIjZjU5ZTBiIiBzdHJva2Utd2lkdGg9IjMuNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBvcGFjaXR5PSIwLjYiLz4KICA8bGluZSB4MT0iMTciIHkxPSI0NCIgeDI9IjQ0IiB5Mj0iNDQiIHN0cm9rZT0iI2Y1OWUwYiIgc3Ryb2tlLXdpZHRoPSIzLjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIgb3BhY2l0eT0iMC4zIi8+CiAgPGNpcmNsZSBjeD0iNDciIGN5PSIyMiIgcj0iNCIgZmlsbD0iI2Y4NzE3MSIvPgo8L3N2Zz4=';
  var deferredPrompt=null, bannerShown=false;

  function snoozeKey(){return 'axiom-pi-pwa-snoozed';}
  function isSnoozed(){try{var d=localStorage.getItem(snoozeKey());return d&&(Date.now()-parseInt(d))<7*24*3600*1000;}catch(e){return false;}}
  function snooze(){try{localStorage.setItem(snoozeKey(),Date.now());}catch(e){}}

  function makeBanner(html, onInstall, onDismiss){
    if(bannerShown) return;
    bannerShown=true;
    var b=document.createElement('div');
    b.id='pwa-install-banner';
    b.innerHTML=html;
    b.style.cssText='position:fixed;bottom:0;left:0;right:0;z-index:99999;display:flex;align-items:center;gap:12px;padding:14px 18px;padding-bottom:calc(14px + env(safe-area-inset-bottom,0px));background:#111009;border-top:1px solid #2a2720;box-shadow:0 -4px 24px rgba(0,0,0,.6);font-family:sans-serif;transform:translateY(100%);transition:transform .35s cubic-bezier(.34,1.56,.64,1)';
    document.body.appendChild(b);
    requestAnimationFrame(function(){requestAnimationFrame(function(){b.style.transform='translateY(0)';}); });
    var inst=document.getElementById('pwa-btn-install');
    var dism=document.getElementById('pwa-btn-dismiss');
    if(inst&&onInstall) inst.addEventListener('click',function(){onInstall();hideBanner();});
    if(dism) dism.addEventListener('click',function(){snooze();hideBanner();if(onDismiss)onDismiss();});
  }

  function hideBanner(){
    var b=document.getElementById('pwa-install-banner');
    if(!b)return;
    b.style.transform='translateY(100%)';
    setTimeout(function(){if(b&&b.parentNode)b.parentNode.removeChild(b);},400);
  }

  var logoHtml='<img src="'+SVG_ICON+'" width="36" height="36" style="border-radius:8px;flex-shrink:0" alt="AXIOM"/>';
  var titleHtml='<div style="font-family:Syne,sans-serif;font-size:13px;font-weight:800;color:#f59e0b;letter-spacing:2px">INSTALL AXIOM PI</div><div style="font-size:10px;color:#a09070;font-family:Space Mono,monospace;margin-top:2px">Add to Home Screen for quick access</div>';
  var btnDismiss='<button id="pwa-btn-dismiss" style="background:transparent;color:#5a5040;border:1px solid #2a2720;border-radius:6px;padding:7px 10px;font-size:11px;cursor:pointer;flex-shrink:0">✕</button>';

  // Android / Chrome
  window.addEventListener('beforeinstallprompt',function(e){
    e.preventDefault(); deferredPrompt=e;
    if(isSnoozed()) return;
    setTimeout(function(){
      makeBanner(
        '<div style="display:flex;align-items:center;gap:12px;flex:1">'+logoHtml+'<div>'+titleHtml+'</div></div>'+
        '<div style="display:flex;gap:8px;flex-shrink:0"><button id="pwa-btn-install" style="background:#f59e0b;color:#0c0a08;border:none;border-radius:6px;padding:7px 14px;font-family:Space Mono,monospace;font-size:10px;font-weight:700;cursor:pointer;letter-spacing:1px">INSTALL</button>'+btnDismiss+'</div>',
        function(){ if(deferredPrompt){deferredPrompt.prompt();deferredPrompt.userChoice.then(function(){deferredPrompt=null;});} },
        null
      );
    },2500);
  });

  // iOS Safari — show manual instructions
  var isIOS=/iphone|ipad|ipod/i.test(navigator.userAgent||navigator.vendor||window.opera);
  var isStandalone=window.navigator.standalone===true||window.matchMedia('(display-mode:standalone)').matches;
  if(isIOS&&!isStandalone&&!isSnoozed()){
    setTimeout(function(){
      makeBanner(
        '<div style="display:flex;align-items:center;gap:12px;flex:1">'+logoHtml+
        '<div><div style="font-family:Syne,sans-serif;font-size:13px;font-weight:800;color:#f59e0b;letter-spacing:2px">ADD TO HOME SCREEN</div>'+
        '<div style="font-size:10px;color:#a09070;font-family:Space Mono,monospace;margin-top:2px">Tap <b style="color:#f59e0b">Share ⎋</b> then <b style="color:#f59e0b">Add to Home Screen ＋</b></div></div></div>'+
        btnDismiss,
        null, null
      );
    },3000);
  }
})();
</script>

<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:ital,wght@0,400;0,700;1,400&family=Syne:wght@700;800&display=swap" rel="stylesheet"/>

<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Mono:ital,wght@0,400;0,700;1,400&family=Syne:wght@700;800&display=swap');
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg0:#0c0a08;--bg1:#111009;--bg2:#17140f;--bg3:#1e1c16;
  --border:#2a2720;--border2:#3d3a30;
  --tx0:#e8dfc8;--tx1:#a09070;--tx2:#5a5040;
  --amber:#f59e0b;--amber-dim:#92600a;--amber-faint:#3d2e08;
  --crit:#f87171;--crit-bg:rgba(248,113,113,0.07);
  --err:#fb923c;--err-bg:rgba(251,146,60,0.06);
  --warn:#fbbf24;--warn-bg:rgba(251,191,36,0.04);
  --good:#34d399;
  --ai:#818cf8;--ai-dim:#3730a3;--ai-bg:rgba(129,140,248,0.06);
}
html,body{height:100%;background:var(--bg0);color:var(--tx0);font-family:'Space Mono',monospace;overflow:hidden}
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:var(--bg0)}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:var(--tx2)}

/* ── LAYOUT ── */
#app{display:flex;flex-direction:column;height:100dvh;overflow:hidden}
#topbar{height:54px;flex-shrink:0;display:flex;align-items:center;border-bottom:1px solid var(--border);background:var(--bg1);padding-right:14px}
#topbar .brand{display:flex;align-items:center;gap:12px;padding:0 16px;height:100%;border-right:1px solid var(--border)}
#topbar .brand-text{font-family:'Syne',sans-serif;font-size:15px;font-weight:800;color:var(--amber);letter-spacing:4px}
#topbar .brand-sub{font-size:7px;color:var(--tx2);letter-spacing:4px;margin-top:1px}
#topbar .breadcrumb{display:flex;align-items:center;gap:8px;margin-left:16px}
#topbar .breadcrumb-label{font-family:'Syne',sans-serif;font-size:11px;font-weight:700;letter-spacing:2px}
#topbar .spacer{flex:1}
#topbar .clock{margin-right:16px;font-size:10px;color:var(--tx1);letter-spacing:1px;display:flex;align-items:center;gap:10px}
#topbar .clock .time{color:var(--tx0)}
#topbar .clock .date{color:var(--tx2);font-size:9px}
#topbar .controls{display:flex;gap:6px;align-items:center}

.pill-btn{display:flex;align-items:center;gap:5px;padding:5px 11px;border-radius:7px;cursor:pointer;background:transparent;border:1px solid var(--border2);color:var(--tx1);font-family:'Space Mono',monospace;font-size:9px;letter-spacing:.5px;transition:all .15s}
.pill-btn:hover{filter:brightness(1.2)}
.pill-btn.active{background:rgba(0,0,0,0);border-color:transparent}

#body{display:flex;flex:1;overflow:hidden}

/* ── SIDEBAR ── */
#sidebar{width:188px;flex-shrink:0;border-right:1px solid var(--border);background:var(--bg1);display:flex;flex-direction:column;overflow-y:auto;overflow-x:hidden;padding:10px 6px;transition:width .2s ease}
#sidebar.collapsed{width:52px}
.side-section-label{font-size:7px;color:var(--tx2);letter-spacing:3px;padding:2px 8px 10px;text-transform:uppercase;opacity:1;transition:opacity .15s;white-space:nowrap}
#sidebar.collapsed .side-section-label{opacity:0}
.nav-item{display:flex;align-items:center;gap:10px;justify-content:flex-start;padding:8px 10px;border-radius:8px;margin-bottom:2px;width:100%;background:transparent;border:1px solid transparent;color:var(--tx1);cursor:pointer;text-align:left;transition:all .12s}
#sidebar.collapsed .nav-item{gap:0;justify-content:center;padding:8px}
.nav-item:hover{background:var(--bg3);border-color:var(--border2)}
.nav-item.active{border-color:transparent}
.nav-icon-box{width:30px;height:30px;border-radius:7px;flex-shrink:0;background:var(--bg2);border:1px solid var(--border);display:flex;align-items:center;justify-content:center;transition:all .12s}
.nav-label{font-family:'Syne',sans-serif;font-size:11px;font-weight:700;letter-spacing:.5px;white-space:nowrap}
.nav-desc{font-family:'Space Mono',monospace;font-size:8px;color:var(--tx2);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#sidebar.collapsed .nav-text{display:none}
.nav-active-bar{width:2px;height:16px;border-radius:1px;flex-shrink:0}
#sidebar.collapsed .nav-active-bar{display:none}
.nav-divider{margin:8px 6px;border-top:1px solid var(--border)}
.side-collapse-btn{display:flex;align-items:center;justify-content:center;margin:6px 2px 0;padding:8px;border-radius:7px;background:transparent;border:1px solid var(--border);color:var(--tx2);cursor:pointer;transition:all .12s;width:calc(100% - 4px)}
.side-collapse-btn:hover{background:var(--bg3);color:var(--tx1)}

/* ── MAIN ── */
#main{flex:1;display:flex;flex-direction:column;overflow:hidden}

/* ── TOOLBAR ── */
#toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:7px 14px;border-bottom:1px solid var(--border);background:var(--bg1);flex-shrink:0}
.sev-chip{display:flex;align-items:center;gap:4px;padding:2px 9px;border-radius:20px;font-family:'Space Mono',monospace;font-size:8px;letter-spacing:.5px}
.sev-chip .dot{width:5px;height:5px;border-radius:50%}
.line-count{font-size:9px;color:var(--tx2)}
.toolbar-spacer{margin-left:auto;display:flex;gap:8px;align-items:center}
.search-box{display:flex;align-items:center;gap:8px;background:var(--bg0);border:1px solid var(--border2);border-radius:8px;padding:5px 12px;min-width:160px}
.search-box input{background:none;border:none;outline:none;color:var(--tx0);font-size:11px;width:100%;min-width:0;font-family:'Space Mono',monospace}
.search-box input::placeholder{color:var(--tx2)}
.lines-select{background:var(--bg0);border:1px solid var(--border2);color:var(--tx1);border-radius:8px;padding:5px 10px;font-size:10px;cursor:pointer;font-family:'Space Mono',monospace;outline:none}

/* ── AI PANEL ── */
#ai-panel-wrap{padding:8px 14px 0;flex-shrink:0}
.panel-card{border-radius:10px;overflow:hidden;background:var(--bg1);display:flex;flex-direction:column;margin-bottom:8px}
.panel-header{display:flex;align-items:center;gap:8px;padding:9px 14px;border-bottom:1px solid var(--border);flex-shrink:0}
.panel-header-label{font-family:'Syne',sans-serif;font-size:10px;font-weight:700;letter-spacing:3px}
.panel-close{margin-left:auto;background:none;border:none;cursor:pointer;padding:4px;display:flex;align-items:center;justify-content:center;border-radius:6px;transition:background .12s;color:var(--tx2)}
.panel-close:hover{background:var(--bg3)}
.panel-body{padding:10px 14px}
.ai-stream{font-family:'Space Mono',monospace;font-size:11px;line-height:1.8}
.ai-stream p{margin-bottom:4px}

/* ── CHAT ── */
.chat-messages{flex:1;overflow-y:auto;padding:12px 14px;display:flex;flex-direction:column;gap:10px}
.chat-bubble{max-width:90%;padding:9px 13px;font-family:'Space Mono',monospace;font-size:11px;line-height:1.8;white-space:pre-wrap;word-break:break-word}
.chat-bubble.user{align-self:flex-end;background:rgba(245,158,11,.1);border:1px solid rgba(146,96,10,.33);border-radius:12px 12px 3px 12px;color:var(--amber)}
.chat-bubble.ai{align-self:flex-start;background:var(--ai-bg);border:1px solid rgba(55,48,163,.27);border-radius:3px 12px 12px 12px;color:var(--tx0)}
.chat-author{font-family:'Space Mono',monospace;font-size:8px;color:var(--tx2)}
.quick-prompts{display:flex;gap:5px;flex-wrap:wrap;padding:0 14px 8px}
.quick-btn{font-family:'Space Mono',monospace;font-size:9px;padding:4px 10px;border-radius:20px;background:transparent;border:1px solid var(--border2);color:var(--tx1);cursor:pointer;transition:all .12s}
.quick-btn:hover{border-color:rgba(129,140,248,.53);color:var(--ai)}
.chat-input-row{display:flex;align-items:center;gap:8px;padding:10px 14px;border-top:1px solid var(--border)}
.chat-input{flex:1;background:var(--bg0);border:1px solid var(--border2);border-radius:8px;padding:8px 12px;font-family:'Space Mono',monospace;font-size:11px;color:var(--tx0);outline:none;transition:border-color .15s}
.chat-input:focus{border-color:rgba(129,140,248,.53)}
.chat-send{width:36px;height:36px;border-radius:8px;display:flex;align-items:center;justify-content:center;cursor:pointer;background:transparent;border:1px solid var(--border);transition:all .12s;flex-shrink:0}
.chat-send.ready{background:rgba(129,140,248,.1);border-color:rgba(129,140,248,.33)}

/* ── LOG AREA ── */
#log-area{flex:1;overflow-y:auto;position:relative}
#log-area::before{content:"";position:absolute;inset:0;pointer-events:none;z-index:1;background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,.04) 2px,rgba(0,0,0,.04) 4px)}
.log-loading{display:flex;align-items:center;justify-content:center;height:120px;gap:10px;color:var(--amber-dim);font-size:11px}
.log-empty{padding:48px;text-align:center;color:var(--tx2);font-size:11px}
.log-error-msg{margin:16px;padding:12px 16px;background:rgba(248,113,113,.08);border:1px solid rgba(248,113,113,.3);border-radius:8px;font-size:11px;color:var(--err)}
.log-line{display:flex;border-left:2px solid transparent}
.log-gutter{flex-shrink:0;width:46px;text-align:right;padding-right:12px;padding-left:8px;color:var(--tx2);font-size:9px;user-select:none;border-right:1px solid var(--border);line-height:1.7}
.log-text{flex:1;padding-left:12px;padding-right:14px;white-space:pre-wrap;word-break:break-all;font-size:10.5px;line-height:1.7}

/* ── SMART PANEL ── */
.smart-drive-card{border-radius:8px;overflow:hidden;background:var(--bg1);margin-bottom:8px}
.smart-drive-btn{display:flex;align-items:center;gap:12px;padding:10px 14px;width:100%;background:transparent;border:none;cursor:pointer;text-align:left}
.smart-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.smart-drive-name{flex:1;font-family:'Space Mono',monospace;font-size:11px;color:var(--tx0)}
.smart-drive-mount{font-size:9px;color:var(--tx2);margin-left:4px}
.smart-drive-status{font-family:'Space Mono',monospace;font-size:10px}
.smart-raw{margin:0;padding:10px 14px;font-family:'Space Mono',monospace;font-size:9px;color:var(--tx1);background:var(--bg0);max-height:200px;overflow-y:auto;white-space:pre-wrap;line-height:1.7}
.smart-unsupported-label{font-size:9px;color:var(--tx2);letter-spacing:2px;padding:4px 2px 6px;font-family:'Space Mono',monospace}

/* ── SYSMON ── */
.sysmon-line{padding:1px 16px;border-left:2px solid transparent;font-size:10px;line-height:1.75}

/* ── FOOTER ── */
#footer{display:flex;align-items:center;justify-content:space-between;padding:4px 14px;border-top:1px solid var(--border);background:var(--bg1);flex-shrink:0;font-size:9px;color:var(--tx2)}
#footer .footer-left{display:flex;align-items:center;gap:10px}
#footer .footer-right{display:flex;gap:14px}
#footer .v{font-family:'Syne',sans-serif;font-weight:800;color:var(--amber-dim);letter-spacing:3px;font-size:8px}

/* ── BOTTOM NAV (mobile) ── */
#bottom-nav{display:none}

/* ── TYPING ── */
.typing{display:flex;gap:4px;align-items:center;height:18px}
.typing span{width:5px;height:5px;border-radius:50%;background:var(--tx2);animation:dot-bounce 1.2s ease-in-out infinite}
.typing span:nth-child(2){animation-delay:.2s}
.typing span:nth-child(3){animation-delay:.4s}

@keyframes dot-bounce{0%,100%{transform:translateY(0);opacity:.3}50%{transform:translateY(-4px);opacity:1}}
@keyframes fade-in{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.2}}
@keyframes glow-crit{0%,100%{box-shadow:0 0 0 transparent}50%{box-shadow:0 0 6px rgba(248,113,113,.27)}}
.log-line{animation:fade-in .1s ease both}

/* ── MOBILE ── */
@media(max-width:700px){
  #sidebar{display:none!important}
  #bottom-nav{display:flex!important}
  .logo-text-wrap{display:none!important}
  .top-controls-full{display:none!important}
  #toolbar .lines-select{display:none!important}
  .log-gutter{width:34px!important;padding-right:6px!important;padding-left:4px!important;font-size:8px!important}
  .log-text{padding-left:8px!important;padding-right:8px!important}
  #footer{display:none!important}
  #ai-panel-wrap{padding:6px 8px 0!important}
  #topbar{height:52px!important}
}
</style>
</head>
<body>

<!-- ── SPLASH SCREEN ── -->
<div id="axiom-splash" style="position:fixed;inset:0;z-index:9999;background:#0c0a08;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:0;transition:opacity .6s ease">
  <svg id="splash-hex" viewBox="0 0 64 64" fill="none" style="width:90px;height:90px;animation:hex-breathe 2s ease-in-out infinite">
    <rect width="64" height="64" rx="11" fill="#0c0a08"/>
    <polygon points="32,6 56,19 56,45 32,58 8,45 8,19" stroke="#f59e0b" stroke-width="2.2" fill="none" stroke-linejoin="round"/>
    <line x1="17" y1="28" x2="47" y2="28" stroke="#f59e0b" stroke-width="3.5" stroke-linecap="round"/>
    <line x1="17" y1="36" x2="41" y2="36" stroke="#f59e0b" stroke-width="3.5" stroke-linecap="round" opacity=".55"/>
    <line x1="17" y1="44" x2="44" y2="44" stroke="#f59e0b" stroke-width="3.5" stroke-linecap="round" opacity=".25"/>
    <circle cx="47" cy="22" r="4" fill="#f87171"/>
  </svg>
  <div style="margin-top:24px;font-family:'Syne',sans-serif;font-size:32px;font-weight:800;color:#f59e0b;letter-spacing:12px;text-shadow:0 0 40px #f59e0b88,0 0 80px #f59e0b22">AXIOM</div>
  <div style="margin-top:6px;font-family:'Space Mono',monospace;font-size:9px;letter-spacing:5px;color:#3d3228;text-transform:uppercase">Pi Node · Log Agent · v1.1</div>
  <div style="margin-top:32px;width:180px;height:1px;background:#1e1c18;border-radius:2px;overflow:hidden">
    <div style="height:100%;background:linear-gradient(90deg,#f59e0b,#fbbf24);animation:fill-run 1.8s ease-out forwards;border-radius:2px"></div>
  </div>
  <div style="margin-top:18px;display:flex;gap:7px">
    <div style="width:4px;height:4px;border-radius:50%;background:#f59e0b;animation:dot-pop 1.6s ease-in-out infinite"></div>
    <div style="width:4px;height:4px;border-radius:50%;background:#f59e0b;animation:dot-pop 1.6s .18s ease-in-out infinite"></div>
    <div style="width:4px;height:4px;border-radius:50%;background:#f59e0b;animation:dot-pop 1.6s .36s ease-in-out infinite"></div>
  </div>
</div>

<!-- ── INSTALL BANNER (Android A2HS + iOS instructions) ── -->
<div id="install-banner" style="display:none;position:fixed;bottom:0;left:0;right:0;z-index:8888;padding:12px 16px calc(12px + env(safe-area-inset-bottom));background:#1a1710;border-top:1px solid rgba(245,158,11,.25);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px)">
  <div style="display:flex;align-items:center;gap:12px;max-width:480px;margin:0 auto">
    <svg viewBox="0 0 64 64" fill="none" style="width:36px;height:36px;flex-shrink:0">
      <rect width="64" height="64" rx="11" fill="#0c0a08"/>
      <polygon points="32,6 56,19 56,45 32,58 8,45 8,19" stroke="#f59e0b" stroke-width="2.2" fill="none" stroke-linejoin="round"/>
      <line x1="17" y1="28" x2="47" y2="28" stroke="#f59e0b" stroke-width="3.5" stroke-linecap="round"/>
      <line x1="17" y1="36" x2="41" y2="36" stroke="#f59e0b" stroke-width="3.5" stroke-linecap="round" opacity=".55"/>
      <circle cx="47" cy="22" r="4" fill="#f87171"/>
    </svg>
    <div style="flex:1;min-width:0">
      <div style="font-family:'Syne',sans-serif;font-size:13px;font-weight:700;color:#e8dfc8" id="install-title">Install AXIOM Pi</div>
      <div style="font-family:'Space Mono',monospace;font-size:10px;color:#a09070;margin-top:2px" id="install-sub">Add to home screen for quick access</div>
    </div>
    <button id="install-btn" onclick="doInstall()" style="font-family:'Space Mono',monospace;font-size:10px;padding:7px 14px;border-radius:6px;background:rgba(245,158,11,.15);border:1px solid rgba(245,158,11,.4);color:#f59e0b;cursor:pointer;white-space:nowrap;letter-spacing:.5px">INSTALL</button>
    <button onclick="dismissBanner()" style="background:none;border:none;color:#5a5040;cursor:pointer;padding:4px;font-size:18px;line-height:1">×</button>
  </div>
</div>

<style>
/* Safe area padding */
body {
  padding-top: env(safe-area-inset-top);
  padding-right: env(safe-area-inset-right);
  padding-bottom: env(safe-area-inset-bottom);
  padding-left: env(safe-area-inset-left);
}
@keyframes hex-breathe{0%,100%{filter:drop-shadow(0 0 8px #f59e0b44);transform:scale(1)}50%{filter:drop-shadow(0 0 28px #f59e0baa);transform:scale(1.06)}}
@keyframes fill-run{from{width:0}to{width:100%}}
@keyframes dot-pop{0%,100%{opacity:.2;transform:scale(.7)}50%{opacity:1;transform:scale(1.4)}}
</style>

<script>
// Splash hide — called after first data loads
window.__hideSplash = function() {
  const s = document.getElementById('axiom-splash');
  if (!s) return;
  s.style.opacity = '0';
  s.style.pointerEvents = 'none';
  setTimeout(() => s && s.parentNode && s.parentNode.removeChild(s), 700);
};

// Install banner logic
let _bannerDismissed = false;
try { _bannerDismissed = !!sessionStorage.getItem('axiom-banner-dismissed'); } catch(e) {}

function showInstallBanner() {
  if (_bannerDismissed) return;
  const banner = document.getElementById('install-banner');
  if (!banner) return;

  const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
  const isInStandalone = window.navigator.standalone || window.matchMedia('(display-mode: standalone)').matches;
  if (isInStandalone) return; // already installed

  if (isIOS) {
    // iOS: show share instructions
    document.getElementById('install-title').textContent = 'Add to Home Screen';
    document.getElementById('install-sub').textContent = 'Tap Share ⬆ then "Add to Home Screen"';
    document.getElementById('install-btn').textContent = 'GOT IT';
    document.getElementById('install-btn').onclick = dismissBanner;
  } else if (!window.__a2hs) {
    return; // No prompt available (already installed or not supported)
  }

  banner.style.display = 'block';
}

function doInstall() {
  if (window.__a2hs) {
    window.__a2hs.prompt();
    window.__a2hs.userChoice.then(r => {
      if (r.outcome === 'accepted') dismissBanner();
    });
  } else {
    dismissBanner();
  }
}

function dismissBanner() {
  try { sessionStorage.setItem('axiom-banner-dismissed', '1'); } catch(e) {}
  _bannerDismissed = true;
  const b = document.getElementById('install-banner');
  if (b) { b.style.transition = 'opacity .3s'; b.style.opacity = '0'; setTimeout(() => b.remove(), 300); }
}

window.__showInstallBanner = showInstallBanner;

// iOS: show banner after delay if not standalone
setTimeout(() => {
  const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
  const isStandalone = window.navigator.standalone || window.matchMedia('(display-mode: standalone)').matches;
  if (isIOS && !isStandalone) showInstallBanner();
}, 4000);
</script>

<div id="app">

  <!-- TOP BAR -->
  <header id="topbar">
    <div class="brand">
      <svg width="32" height="32" viewBox="0 0 64 64" fill="none">
        <rect width="64" height="64" rx="11" fill="#0c0a08"/>
        <polygon points="32,6 56,19 56,45 32,58 8,45 8,19" stroke="#f59e0b" stroke-width="2.2" fill="none" stroke-linejoin="round"/>
        <line x1="17" y1="28" x2="47" y2="28" stroke="#f59e0b" stroke-width="3.5" stroke-linecap="round"/>
        <line x1="17" y1="36" x2="41" y2="36" stroke="#f59e0b" stroke-width="3.5" stroke-linecap="round" opacity=".55"/>
        <line x1="17" y1="44" x2="44" y2="44" stroke="#f59e0b" stroke-width="3.5" stroke-linecap="round" opacity=".25"/>
        <circle cx="47" cy="22" r="4" fill="#f87171"/>
      </svg>
      <div class="logo-text-wrap">
        <div class="brand-text">AXIOM</div>
        <div class="brand-sub">PI NODE</div>
      </div>
    </div>

    <div class="breadcrumb" id="breadcrumb">
      <!-- filled by JS -->
    </div>

    <div class="spacer"></div>

    <div class="clock top-controls-full">
      <span class="time" id="clock-time">--:--:--</span>
      <span class="date" id="clock-date"></span>
    </div>

    <div class="controls top-controls-full">
      <button class="pill-btn" id="btn-auto" onclick="toggleAuto()" title="Enable live refresh">
        <svg id="live-icon" width="13" height="13" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="3" fill="#a09070"/><circle cx="8" cy="8" r="5.5" stroke="#a09070" stroke-width="1" opacity=".4"/><circle cx="8" cy="8" r="7.5" stroke="#a09070" stroke-width=".7" opacity=".15"/></svg>
        <span id="btn-auto-label">AUTO</span>
      </button>
      <button class="pill-btn" id="btn-analyze" onclick="toggleAnalyze()" title="Auto-analyze logs with AI">
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="5.5" stroke="#a09070" stroke-width="1.4"/><path d="M5.5 9.5c.7 1 1.5 1.5 2.5 1.5s1.8-.5 2.5-1.5" stroke="#a09070" stroke-width="1.3" stroke-linecap="round"/><circle cx="6" cy="7" r="1" fill="#a09070"/><circle cx="10" cy="7" r="1" fill="#a09070"/></svg>
        <span>ANALYZE</span>
      </button>
      <button class="pill-btn" id="btn-chat" onclick="toggleChat()" title="Chat with AI about logs">
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M2 3h12a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1H5l-3 2V4a1 1 0 0 1 1-1z" stroke="#a09070" stroke-width="1.4"/><line x1="5" y1="7" x2="11" y2="7" stroke="#a09070" stroke-width="1.3" stroke-linecap="round"/><line x1="5" y1="9.5" x2="9" y2="9.5" stroke="#a09070" stroke-width="1.3" stroke-linecap="round"/></svg>
        <span>ASK AI</span>
      </button>
      <button class="pill-btn" onclick="fetchLogs()" title="Refresh">
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M13 6A5.5 5.5 0 1 0 11 11" stroke="#a09070" stroke-width="1.5" stroke-linecap="round"/><polyline points="13,2 13,6 9,6" stroke="#a09070" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/></svg>
      </button>
    </div>
  </header>

  <div id="body">

    <!-- SIDEBAR -->
    <nav id="sidebar">
      <div class="side-section-label">Sources</div>
      <div id="nav-items"></div>
      <div style="flex:1"></div>
      <button class="side-collapse-btn" onclick="toggleSidebar()" id="collapse-btn" title="Collapse">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" id="collapse-icon">
          <polyline points="11,4 5,8 11,12" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>
    </nav>

    <!-- MAIN CONTENT -->
    <div id="main">

      <!-- TOOLBAR -->
      <div id="toolbar">
        <div id="sev-chips"></div>
        <span class="line-count" id="line-count-label"></span>
        <div id="sev-bar" style="display:flex;align-items:center;gap:4px;height:16px"></div>
        <div class="toolbar-spacer">
          <div class="search-box">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><circle cx="6.5" cy="6.5" r="4" stroke="#5a5040" stroke-width="1.5"/><line x1="9.5" y1="9.5" x2="14" y2="14" stroke="#5a5040" stroke-width="1.5" stroke-linecap="round"/></svg>
            <input id="search-input" placeholder="filter…" oninput="applyFilter()" />
            <button id="search-clear" onclick="clearSearch()" style="display:none;background:none;border:none;color:var(--tx2);cursor:pointer;font-size:14px;line-height:1;padding:0">×</button>
          </div>
          <select class="lines-select" id="lines-select" onchange="onLinesChange()">
            <option value="100">100 lines</option>
            <option value="200" selected>200 lines</option>
            <option value="500">500 lines</option>
            <option value="1000">1000 lines</option>
            <option value="2000">2000 lines</option>
          </select>
        </div>
      </div>

      <!-- AI PANEL -->
      <div id="ai-panel-wrap" style="display:none"></div>

      <!-- LOG AREA -->
      <div id="log-area">
        <div class="log-empty" style="color:var(--tx2)">Select a source to begin</div>
      </div>

      <!-- FOOTER -->
      <footer id="footer">
        <div class="footer-left">
          <svg width="16" height="16" viewBox="0 0 64 64" fill="none"><rect width="64" height="64" rx="11" fill="#0c0a08"/><polygon points="32,6 56,19 56,45 32,58 8,45 8,19" stroke="#f59e0b" stroke-width="2.2" fill="none" stroke-linejoin="round"/><line x1="17" y1="28" x2="47" y2="28" stroke="#f59e0b" stroke-width="3.5" stroke-linecap="round"/><line x1="17" y1="36" x2="41" y2="36" stroke="#f59e0b" stroke-width="3.5" stroke-linecap="round" opacity=".55"/><line x1="17" y1="44" x2="44" y2="44" stroke="#f59e0b" stroke-width="3.5" stroke-linecap="round" opacity=".25"/><circle cx="47" cy="22" r="4" fill="#f87171"/></svg>
          <span class="v">AXIOM</span>
          <span>v1.1</span>
          <span id="footer-live" style="display:none;color:var(--good)"><span style="animation:pulse 1.4s infinite">●</span> live</span>
        </div>
        <div class="footer-right">
          <span id="footer-fetched"></span>
          <span id="footer-crit" style="color:var(--tx2)">0 crit</span>
          <span id="footer-err" style="color:var(--tx2)">0 err</span>
          <span id="footer-warn" style="color:var(--tx2)">0 warn</span>
          <span id="footer-lines">0 lines</span>
        </div>
      </footer>
    </div>
  </div>

  <!-- BOTTOM NAV (mobile) -->
  <nav id="bottom-nav" style="flex-shrink:0;border-top:1px solid var(--border);background:var(--bg1);align-items:stretch;padding-bottom:env(safe-area-inset-bottom,0);z-index:100">
    <div id="bottom-nav-items" style="display:flex"></div>
  </nav>
</div>

<script>
// ─── CONFIG ──────────────────────────────────────────────────────────────────
const API = '';  // same-origin — served by pi_agent.py

// ─── SOURCES ─────────────────────────────────────────────────────────────────
const SOURCES = [
  { id:'syslog', label:'System',    color:'#f59e0b', desc:'Daemons · kernel events',
    icon:`<circle cx="8" cy="8" r="5.5" stroke="COL" stroke-width="1.4"/><circle cx="8" cy="8" r="2" fill="COL"/><line x1="8" y1="2" x2="8" y2="4" stroke="COL" stroke-width="1.4" stroke-linecap="round"/><line x1="8" y1="12" x2="8" y2="14" stroke="COL" stroke-width="1.4" stroke-linecap="round"/><line x1="2" y1="8" x2="4" y2="8" stroke="COL" stroke-width="1.4" stroke-linecap="round"/><line x1="12" y1="8" x2="14" y2="8" stroke="COL" stroke-width="1.4" stroke-linecap="round"/>` },
  { id:'kernel', label:'Kernel',    color:'#fbbf24', desc:'Hardware · drivers',
    icon:`<rect x="2" y="4" width="12" height="8" rx="1.5" stroke="COL" stroke-width="1.4"/><line x1="5" y1="7" x2="5" y2="9" stroke="COL" stroke-width="1.4" stroke-linecap="round"/><line x1="8" y1="6" x2="8" y2="10" stroke="COL" stroke-width="1.4" stroke-linecap="round"/><line x1="11" y1="7" x2="11" y2="9" stroke="COL" stroke-width="1.4" stroke-linecap="round"/>` },
  { id:'auth',   label:'Auth',      color:'#34d399', desc:'SSH · sudo · logins',
    icon:`<rect x="4" y="7" width="8" height="6" rx="1.5" stroke="COL" stroke-width="1.4"/><path d="M5.5 7V5.5a2.5 2.5 0 0 1 5 0V7" stroke="COL" stroke-width="1.4" stroke-linecap="round"/><circle cx="8" cy="10" r="1" fill="COL"/>` },
  { id:'docker', label:'Docker',    color:'#fb923c', desc:'Container logs',
    icon:`<rect x="1" y="6" width="4" height="3" rx=".7" stroke="COL" stroke-width="1.3"/><rect x="6" y="6" width="4" height="3" rx=".7" stroke="COL" stroke-width="1.3"/><rect x="11" y="6" width="4" height="3" rx=".7" stroke="COL" stroke-width="1.3"/><rect x="6" y="2" width="4" height="3" rx=".7" stroke="COL" stroke-width="1.3"/><path d="M2 9c0 3 2 4 6 4s6-1 6-4" stroke="COL" stroke-width="1.3"/>` },
  { id:'disk',   label:'Disk',      color:'#818cf8', desc:'Storage · I/O',
    icon:`<ellipse cx="8" cy="5" rx="5.5" ry="2.5" stroke="COL" stroke-width="1.4"/><path d="M2.5 5v6c0 1.38 2.46 2.5 5.5 2.5s5.5-1.12 5.5-2.5V5" stroke="COL" stroke-width="1.4"/><line x1="2.5" y1="9" x2="13.5" y2="9" stroke="COL" stroke-width="1.2" opacity=".5"/>` },
  { id:'boot',   label:'Boot',      color:'#94a3b8', desc:'Boot · dmesg',
    icon:`<polyline points="3,12 8,4 13,12" stroke="COL" stroke-width="1.5" stroke-linejoin="round"/><line x1="5.5" y1="9" x2="10.5" y2="9" stroke="COL" stroke-width="1.5" stroke-linecap="round"/>` },
  { id:'casaos', label:'CasaOS',    color:'#38bdf8', desc:'CasaOS services',
    icon:`<rect x="2" y="3" width="12" height="10" rx="2" stroke="COL" stroke-width="1.4"/><line x1="5" y1="7" x2="11" y2="7" stroke="COL" stroke-width="1.3" stroke-linecap="round"/><line x1="5" y1="9.5" x2="9" y2="9.5" stroke="COL" stroke-width="1.3" stroke-linecap="round"/>` },
  { id:'smart',  label:'S.M.A.R.T', color:'#34d399', desc:'Drive health',
    icon:`<rect x="2" y="3" width="12" height="10" rx="2" stroke="COL" stroke-width="1.4"/><circle cx="8" cy="8" r="2.5" stroke="COL" stroke-width="1.3"/><line x1="10" y1="6" x2="12.5" y2="3.5" stroke="COL" stroke-width="1.3" stroke-linecap="round"/>` },
  { id:'_sysmon', label:'Sysmon',   color:'#a09070', desc:'App activity', divider:true,
    icon:`<rect x="2" y="2" width="12" height="12" rx="2" stroke="COL" stroke-width="1.4"/><line x1="5" y1="6" x2="11" y2="6" stroke="COL" stroke-width="1.3" stroke-linecap="round"/><line x1="5" y1="8.5" x2="9" y2="8.5" stroke="COL" stroke-width="1.3" stroke-linecap="round"/><line x1="5" y1="11" x2="10" y2="11" stroke="COL" stroke-width="1.3" stroke-linecap="round"/>` },
];

// ─── STATE ───────────────────────────────────────────────────────────────────
let activeId    = 'syslog';
let logData     = null;
let filteredEntries = [];
let lineCount   = 200;
let autoRefresh = false;
let aiMode      = null;   // null | 'analyze' | 'chat'
let sideOpen    = true;
let autoTimer   = null;
let searchVal   = '';
let chatMessages = [];
let chatBusy    = false;
let chatES      = null;
let analyzeKey  = 0;

// ─── CLASSIFY ────────────────────────────────────────────────────────────────
function classify(line) {
  const l = line.toLowerCase();
  if (/\b(critical|panic|fatal|emergency|oom.kill|segfault|kernel.bug)\b/.test(l)) return 'critical';
  if (/\b(error|err\b|failed|failure|denied|refused|timeout|abort|corrupt|cannot)\b/.test(l)) return 'error';
  if (/\b(warn|warning|deprecated|retrying|slow|delay|high\s+load)\b/.test(l)) return 'warn';
  if (/\b(success|started|enabled|connected|accepted|complete|ok\b|running)\b/.test(l)) return 'good';
  return 'normal';
}
const COLORS = {
  critical: { color:'#f87171', bg:'rgba(248,113,113,0.07)', bar:'#f87171' },
  error:    { color:'#fb923c', bg:'rgba(251,146,60,0.06)',  bar:'#fb923c' },
  warn:     { color:'#fbbf24', bg:'rgba(251,191,36,0.04)',  bar:'#fbbf24' },
  good:     { color:'#34d399', bg:'transparent', bar:'transparent' },
  normal:   { color:'#e8dfc8', bg:'transparent', bar:'transparent' },
};

// ─── CLOCK ───────────────────────────────────────────────────────────────────
setInterval(() => {
  const now = new Date();
  document.getElementById('clock-time').textContent = now.toLocaleTimeString();
  document.getElementById('clock-date').textContent = now.toLocaleDateString(undefined,{weekday:'short',month:'short',day:'numeric'});
}, 1000);
(function(){ const now=new Date(); document.getElementById('clock-time').textContent=now.toLocaleTimeString(); document.getElementById('clock-date').textContent=now.toLocaleDateString(undefined,{weekday:'short',month:'short',day:'numeric'}); })();

// ─── SIDEBAR RENDER ──────────────────────────────────────────────────────────
function mkIcon(src, col, sz=15) {
  return `<svg width="${sz}" height="${sz}" viewBox="0 0 16 16" fill="none">${src.replace(/COL/g, col)}</svg>`;
}

function renderSidebar() {
  const container = document.getElementById('nav-items');
  container.innerHTML = SOURCES.map(s => {
    const active = s.id === activeId;
    const col = active ? s.color : '#5a5040';
    const bg  = active ? `${s.color}18` : 'var(--bg2)';
    const border = active ? `${s.color}44` : 'var(--border)';
    return `
      ${s.divider ? '<div class="nav-divider"></div>' : ''}
      <button class="nav-item${active?' active':''}" onclick="switchSource('${s.id}')" title="${s.label}"
        style="background:${active?s.color+'10':'transparent'};border-color:${active?s.color+'38':'transparent'};color:${active?s.color:'var(--tx1)'}">
        <div class="nav-icon-box" style="background:${bg};border-color:${border}">${mkIcon(s.icon, col)}</div>
        <div class="nav-text">
          <div class="nav-label">${s.label}</div>
          <div class="nav-desc">${s.desc}</div>
        </div>
        ${active ? `<div class="nav-active-bar" style="background:${s.color};box-shadow:0 0 6px ${s.color}88"></div>` : ''}
      </button>`;
  }).join('');

  // Bottom nav (mobile)
  document.getElementById('bottom-nav-items').innerHTML = SOURCES.map(s => {
    const active = s.id === activeId;
    return `<button onclick="switchSource('${s.id}')" style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;padding:8px 2px 6px;background:transparent;border:none;cursor:pointer;border-top:2px solid ${active?s.color:'transparent'};transition:all .12s;min-width:0">
      <div style="width:28px;height:28px;border-radius:7px;background:${active?s.color+'18':'var(--bg2)'};display:flex;align-items:center;justify-content:center">${mkIcon(s.icon, active?s.color:'#5a5040', 14)}</div>
      <span style="font-size:7px;color:${active?s.color:'var(--tx2)'};white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%">${s.label}</span>
    </button>`;
  }).join('');
}

function updateBreadcrumb() {
  const src = SOURCES.find(s => s.id === activeId);
  if (!src) return;
  document.getElementById('breadcrumb').innerHTML = `
    ${mkIcon(src.icon, src.color, 14)}
    <span class="breadcrumb-label" style="color:${src.color}">${src.label}</span>`;
}

function toggleSidebar() {
  sideOpen = !sideOpen;
  const sb = document.getElementById('sidebar');
  sb.classList.toggle('collapsed', !sideOpen);
  document.getElementById('collapse-icon').innerHTML = sideOpen
    ? `<polyline points="11,4 5,8 11,12" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>`
    : `<polyline points="5,4 11,8 5,12" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>`;
}

// ─── SOURCE SWITCH ────────────────────────────────────────────────────────────
function switchSource(id) {
  activeId = id;
  logData  = null;
  filteredEntries = [];
  searchVal = '';
  document.getElementById('search-input').value = '';
  document.getElementById('search-clear').style.display = 'none';
  aiMode = null;
  renderAiPanel();
  renderSidebar();
  updateBreadcrumb();
  updateToolbarBtns();
  document.getElementById('log-area').innerHTML = '<div class="log-loading"><div class="typing"><span></span><span></span><span></span></div> Loading…</div>';
  fetchLogs();
}

// ─── FETCH LOGS ───────────────────────────────────────────────────────────────
async function fetchLogs() {
  if (activeId === '_sysmon') { fetchSysmon(); return; }
  if (activeId === 'smart')   { fetchSmart();  return; }
  try {
    const r = await fetch(`${API}/api/logs/${activeId}?lines=${lineCount}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    logData = await r.json();
    applyFilter();
  } catch(e) {
    document.getElementById('log-area').innerHTML = `<div class="log-error-msg">✗ ${e.message}</div>`;
  }
}

function applyFilter() {
  if (!logData) return;
  searchVal = document.getElementById('search-input').value;
  document.getElementById('search-clear').style.display = searchVal ? 'block' : 'none';
  const entries = logData.entries || [];
  filteredEntries = searchVal
    ? entries.filter(l => l.toLowerCase().includes(searchVal.toLowerCase()))
    : entries;
  renderLogs();
}

function clearSearch() {
  document.getElementById('search-input').value = '';
  applyFilter();
}

function onLinesChange() {
  lineCount = parseInt(document.getElementById('lines-select').value);
  fetchLogs();
}

// ─── RENDER LOGS ─────────────────────────────────────────────────────────────
function renderLogs() {
  if (!logData) return;
  const entries = logData.entries || [];
  const stats = { critical:0, error:0, warn:0, good:0 };
  filteredEntries.forEach(l => { const t=classify(l); if(stats[t]!==undefined) stats[t]++; });

  // Sev chips
  const chips = document.getElementById('sev-chips');
  chips.innerHTML = [
    stats.critical > 0 ? sevChip('#f87171','CRIT',stats.critical) : '',
    stats.error    > 0 ? sevChip('#fb923c','ERR', stats.error)    : '',
    stats.warn     > 0 ? sevChip('#fbbf24','WARN',stats.warn)     : '',
  ].join('');

  // Line count
  document.getElementById('line-count-label').textContent =
    `${filteredEntries.length}${searchVal ? ` / ${entries.length}` : ''} lines`;

  // Sev bar
  const total = Math.max(stats.critical+stats.error+stats.warn, 1);
  document.getElementById('sev-bar').innerHTML = [
    [stats.critical,'#f87171'],[stats.error,'#fb923c'],[stats.warn,'#fbbf24']
  ].map(([n,c]) => n>0 ? `<div title="${n}" style="height:10px;width:${Math.max(6,n/total*60)}px;background:${c};border-radius:2px;opacity:.85"></div>` : '').join('');

  // Footer
  document.getElementById('footer-fetched').textContent = logData.fetched_at ? `↑ ${new Date(logData.fetched_at).toLocaleTimeString()}` : '';
  document.getElementById('footer-crit').textContent  = `${stats.critical} crit`;
  document.getElementById('footer-crit').style.color  = stats.critical > 0 ? '#f87171' : 'var(--tx2)';
  document.getElementById('footer-err').textContent   = `${stats.error} err`;
  document.getElementById('footer-err').style.color   = stats.error > 0 ? '#fb923c' : 'var(--tx2)';
  document.getElementById('footer-warn').textContent  = `${stats.warn} warn`;
  document.getElementById('footer-warn').style.color  = stats.warn > 0 ? '#fbbf24' : 'var(--tx2)';
  document.getElementById('footer-lines').textContent = `${filteredEntries.length} lines`;

  // Log lines
  if (!filteredEntries.length) {
    document.getElementById('log-area').innerHTML = `<p class="log-empty">${searchVal ? 'No lines match that filter' : 'No log entries found'}</p>`;
    return;
  }
  const html = filteredEntries.map((line, i) => {
    const t = classify(line);
    const s = COLORS[t];
    const esc = line.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    return `<div class="log-line" style="background:${s.bg};border-left-color:${s.bar};color:${s.color}">
      <span class="log-gutter">${i+1}</span>
      <span class="log-text">${esc}</span>
    </div>`;
  }).join('');
  document.getElementById('log-area').innerHTML = html;
}

function sevChip(col, label, n) {
  return `<div class="sev-chip" style="background:${col}12;border:1px solid ${col}38;color:${col}">
    <div class="dot" style="background:${col}${label==='CRIT'?';animation:glow-crit 1.8s infinite':''}"></div>
    ${label} <b>${n}</b>
  </div>`;
}

// ─── SMART ────────────────────────────────────────────────────────────────────
let smartOpen = {};
async function fetchSmart() {
  try {
    const r = await fetch(`${API}/api/logs/smart`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    renderSmart(d);
  } catch(e) {
    document.getElementById('log-area').innerHTML = `<div class="log-error-msg">✗ ${e.message}</div>`;
  }
}

function renderSmart(d) {
  document.getElementById('sev-chips').innerHTML = '';
  document.getElementById('line-count-label').textContent = '';
  document.getElementById('sev-bar').innerHTML = '';

  const { drives=[], unreadable=[] } = d;
  if (!drives.length && !unreadable.length) {
    document.getElementById('log-area').innerHTML = '<p class="log-empty">No drives detected</p>';
    return;
  }

  let html = '<div style="padding:12px 16px;display:flex;flex-direction:column;gap:8px">';

  drives.forEach((dr, i) => {
    const ok = /passed/i.test(dr.health);
    const col = ok ? '#34d399' : '#f87171';
    const label = (dr.health||'').replace(/.*SMART overall-health self-assessment test result:\s*/i,'');
    const key = dr.drive;
    const open = smartOpen[key] || false;
    html += `<div class="smart-drive-card" style="border:1px solid ${col}28">
      <button class="smart-drive-btn" onclick="toggleSmartDrive('${key.replace(/'/g,"\\'")}')">
        <div class="smart-dot" style="background:${col};box-shadow:0 0 10px ${col}88"></div>
        <span class="smart-drive-name">${dr.drive}<span class="smart-drive-mount">${dr.mount ? ' · '+dr.mount : ''}</span></span>
        <span class="smart-drive-status" style="color:${col}">${label||dr.health}</span>
        <span style="color:var(--tx2);font-size:11px;margin-left:8px">${open?'▲':'▼'}</span>
      </button>
      ${open && dr.raw ? `<pre class="smart-raw">${dr.raw.replace(/&/g,'&amp;').replace(/</g,'&lt;')}</pre>` : ''}
    </div>`;
  });

  if (unreadable.length) {
    html += `<div style="margin-top:4px">
      <div class="smart-unsupported-label">UNSUPPORTED / VIRTUAL (${unreadable.length})</div>`;
    unreadable.forEach(dr => {
      html += `<div style="display:flex;align-items:center;gap:10px;padding:7px 14px;border:1px solid var(--border);border-radius:8px;background:var(--bg1);margin-bottom:4px">
        <div class="smart-dot" style="background:var(--tx2)"></div>
        <span style="flex:1;font-family:'Space Mono',monospace;font-size:11px;color:var(--tx2)">${dr.drive}${dr.mount ? ' · '+dr.mount : ''}</span>
        <span style="font-family:'Space Mono',monospace;font-size:10px;color:var(--tx2)">${dr.health}</span>
      </div>`;
    });
    html += '</div>';
  }
  html += '</div>';
  document.getElementById('log-area').innerHTML = html;

  // Store data for re-render on toggle
  window._smartData = d;
}

function toggleSmartDrive(key) {
  smartOpen[key] = !smartOpen[key];
  renderSmart(window._smartData);
}

// ─── SYSMON ───────────────────────────────────────────────────────────────────
async function fetchSysmon() {
  try {
    const r = await fetch(`${API}/api/sysmon-logs`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    const lines = d.lines || [];
    document.getElementById('sev-chips').innerHTML = '';
    document.getElementById('line-count-label').textContent = '';
    document.getElementById('sev-bar').innerHTML = '';
    if (!lines.length) {
      document.getElementById('log-area').innerHTML = '<p class="log-empty">No activity yet</p>';
      return;
    }
    document.getElementById('log-area').innerHTML = '<div style="font-family:\'Space Mono\',monospace;font-size:10px;line-height:1.75">' +
      lines.map(l => {
        const isErr = /\[error\]/i.test(l);
        return `<div class="sysmon-line" style="color:${isErr?'#fb923c':'#a09070'};border-left-color:${isErr?'rgba(251,146,60,.55)':'transparent'}">${l.replace(/&/g,'&amp;').replace(/</g,'&lt;')}</div>`;
      }).join('') + '</div>';
  } catch(e) {
    document.getElementById('log-area').innerHTML = `<div class="log-error-msg">✗ ${e.message}</div>`;
  }
}

// ─── AUTO REFRESH ─────────────────────────────────────────────────────────────
function toggleAuto() {
  autoRefresh = !autoRefresh;
  clearInterval(autoTimer);
  if (autoRefresh) autoTimer = setInterval(fetchLogs, 10000);
  updateToolbarBtns();
  document.getElementById('footer-live').style.display = autoRefresh ? 'inline' : 'none';
}

// ─── AI PANEL ─────────────────────────────────────────────────────────────────
function toggleAnalyze() {
  aiMode = aiMode === 'analyze' ? null : 'analyze';
  if (aiMode === 'analyze') { analyzeKey++; chatMessages = []; }
  updateToolbarBtns();
  renderAiPanel();
}
function toggleChat() {
  aiMode = aiMode === 'chat' ? null : 'chat';
  if (aiMode === 'chat' && chatMessages.length === 0) {
    chatMessages = [{ role:'ai', text:`Ready. Ask me anything about your **${activeId}** logs — errors, patterns, fixes, or security issues.` }];
  }
  updateToolbarBtns();
  renderAiPanel();
}

function renderAiPanel() {
  const wrap = document.getElementById('ai-panel-wrap');
  if (!aiMode || activeId === '_sysmon') { wrap.style.display='none'; wrap.innerHTML=''; return; }
  wrap.style.display = 'block';
  if (aiMode === 'analyze') renderAnalyzePanel();
  else renderChatPanel();
}

// ── ANALYZE ──
let analyzeES = null;
let analyzeBuf = '';
let analyzeDone = false;

function renderAnalyzePanel() {
  const wrap = document.getElementById('ai-panel-wrap');
  wrap.innerHTML = `<div class="panel-card" style="border:1px solid rgba(245,158,11,.18)">
    <div class="panel-header" style="background:rgba(245,158,11,.05)">
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="5.5" stroke="#f59e0b" stroke-width="1.4"/><path d="M5.5 9.5c.7 1 1.5 1.5 2.5 1.5s1.8-.5 2.5-1.5" stroke="#f59e0b" stroke-width="1.3" stroke-linecap="round"/><circle cx="6" cy="7" r="1" fill="#f59e0b"/><circle cx="10" cy="7" r="1" fill="#f59e0b"/></svg>
      <span class="panel-header-label" style="color:#f59e0b">AI ANALYSIS</span>
      <button class="panel-close" onclick="closeAi()">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><line x1="4" y1="4" x2="12" y2="12" stroke="#5a5040" stroke-width="1.8" stroke-linecap="round"/><line x1="12" y1="4" x2="4" y2="12" stroke="#5a5040" stroke-width="1.8" stroke-linecap="round"/></svg>
      </button>
    </div>
    <div class="panel-body ai-stream" id="analyze-body">
      <div class="typing"><span></span><span></span><span></span></div>
    </div>
  </div>`;

  analyzeES && analyzeES.close();
  analyzeBuf = ''; analyzeDone = false;
  const key = analyzeKey;
  const es = new EventSource(`${API}/api/analyze/${activeId}?lines=120`);
  analyzeES = es;
  es.onmessage = e => {
    if (key !== analyzeKey) { es.close(); return; }
    if (e.data === '[DONE]') { analyzeDone = true; es.close(); return; }
    analyzeBuf += e.data;
    const body = document.getElementById('analyze-body');
    if (!body) { es.close(); return; }
    body.innerHTML = analyzeBuf.split('\n').filter(Boolean).map(line => {
      const col = line.startsWith('🔍')?'#f59e0b' : line.startsWith('⚠️')?'#fb923c' : line.startsWith('🔧')?'#34d399' : line.startsWith('📊')?(/CRITICAL/.test(line)?'#f87171':/WARNING/.test(line)?'#fbbf24':'#34d399') : '#e8dfc8';
      return `<p style="color:${col}">${line.replace(/&/g,'&amp;').replace(/</g,'&lt;')}</p>`;
    }).join('');
  };
  es.onerror = () => {
    const body = document.getElementById('analyze-body');
    if (body) body.innerHTML = '<p style="color:#fb923c">Ollama not reachable — is it running on the host?</p>';
    es.close();
  };
}

// ── CHAT ──
function renderChatPanel() {
  const wrap = document.getElementById('ai-panel-wrap');
  const showQuick = chatMessages.length <= 2;
  const QUICK = ["What errors are most critical?","Is there a security concern?","How do I fix the top issue?","What happened in the last hour?"];
  wrap.innerHTML = `<div class="panel-card" style="border:1px solid rgba(129,140,248,.18);max-height:320px">
    <div class="panel-header" style="background:rgba(129,140,248,.05)">
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M2 3h12a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1H5l-3 2V4a1 1 0 0 1 1-1z" stroke="#818cf8" stroke-width="1.4"/><line x1="5" y1="7" x2="11" y2="7" stroke="#818cf8" stroke-width="1.3" stroke-linecap="round"/><line x1="5" y1="9.5" x2="9" y2="9.5" stroke="#818cf8" stroke-width="1.3" stroke-linecap="round"/></svg>
      <span class="panel-header-label" style="color:#818cf8">ASK AI</span>
      <button class="panel-close" onclick="closeAi()">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><line x1="4" y1="4" x2="12" y2="12" stroke="#5a5040" stroke-width="1.8" stroke-linecap="round"/><line x1="12" y1="4" x2="4" y2="12" stroke="#5a5040" stroke-width="1.8" stroke-linecap="round"/></svg>
      </button>
    </div>
    <div class="chat-messages" id="chat-messages">
      ${chatMessages.map(m => `
        <div style="display:flex;flex-direction:column;align-items:${m.role==='user'?'flex-end':'flex-start'};gap:3px">
          <div class="chat-bubble ${m.role}">${m.text ? m.text.replace(/&/g,'&amp;').replace(/</g,'&lt;') : '<div class="typing"><span></span><span></span><span></span></div>'}</div>
          <span class="chat-author">${m.role==='user'?'you':'axiom-ai'}</span>
        </div>`).join('')}
    </div>
    ${showQuick ? `<div class="quick-prompts">${QUICK.map(q=>`<button class="quick-btn" onclick="setInput('${q.replace(/'/g,"\\'")}')">${q}</button>`).join('')}</div>` : ''}
    <div class="chat-input-row">
      <input class="chat-input" id="chat-input" placeholder="Ask about errors, root cause, fixes…" onkeydown="chatKeydown(event)"/>
      <button class="chat-send" id="chat-send-btn" onclick="chatSend()">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><line x1="14" y1="2" x2="7" y2="9" stroke="#5a5040" stroke-width="1.6" stroke-linecap="round"/><polyline points="14,2 9,14 7,9 2,7 14,2" stroke="#5a5040" stroke-width="1.6" stroke-linejoin="round"/></svg>
      </button>
    </div>
  </div>`;
  // Scroll to bottom
  const msgs = document.getElementById('chat-messages');
  if (msgs) msgs.scrollTop = msgs.scrollHeight;
}

function setInput(q) {
  const inp = document.getElementById('chat-input');
  if (inp) { inp.value = q; inp.focus(); }
}

function chatKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); chatSend(); }
}

async function chatSend() {
  if (chatBusy) return;
  const inp = document.getElementById('chat-input');
  if (!inp) return;
  const q = inp.value.trim();
  if (!q) return;
  inp.value = '';
  chatBusy = true;
  const ctx = (logData?.entries || []).slice(-80).join('\n').slice(0, 4000);
  chatMessages.push({ role:'user', text:q }, { role:'ai', text:'', streaming:true });
  renderChatPanel();

  chatES && chatES.close();
  const url = `${API}/api/ask?source=${encodeURIComponent(activeId)}&question=${encodeURIComponent(q)}&context=${encodeURIComponent(ctx)}`;
  const es = new EventSource(url);
  chatES = es;
  let buf = '';
  es.onmessage = e => {
    if (e.data === '[DONE]') {
      chatBusy = false;
      const last = chatMessages[chatMessages.length-1];
      if (last) last.streaming = false;
      renderChatPanel();
      es.close(); chatES = null; return;
    }
    buf += e.data;
    const last = chatMessages[chatMessages.length-1];
    if (last) last.text = buf;
    renderChatPanel();
  };
  es.onerror = () => {
    chatBusy = false;
    const last = chatMessages[chatMessages.length-1];
    if (last) { last.text = '⚠ Ollama not reachable — check that it is running on the host.'; last.streaming = false; }
    renderChatPanel();
    es.close(); chatES = null;
  };
}

function closeAi() {
  aiMode = null;
  analyzeES && analyzeES.close();
  chatES && chatES.close();
  renderAiPanel();
  updateToolbarBtns();
}

// ─── TOOLBAR BTN STATES ───────────────────────────────────────────────────────
function updateToolbarBtns() {
  const btnAuto = document.getElementById('btn-auto');
  const btnAnalyze = document.getElementById('btn-analyze');
  const btnChat = document.getElementById('btn-chat');
  const liveIcon = document.getElementById('live-icon');

  // Auto
  if (autoRefresh) {
    btnAuto.style.background = 'rgba(52,211,153,.15)'; btnAuto.style.borderColor = 'rgba(52,211,153,.33)'; btnAuto.style.color = '#34d399';
    liveIcon.innerHTML = `<circle cx="8" cy="8" r="3" fill="#34d399"/><circle cx="8" cy="8" r="5.5" stroke="#34d399" stroke-width="1" opacity=".4"/><circle cx="8" cy="8" r="7.5" stroke="#34d399" stroke-width=".7" opacity=".15"/>`;
    document.getElementById('btn-auto-label').textContent = 'LIVE';
  } else {
    btnAuto.style.background = 'transparent'; btnAuto.style.borderColor = 'var(--border2)'; btnAuto.style.color = 'var(--tx1)';
    liveIcon.innerHTML = `<circle cx="8" cy="8" r="3" fill="#a09070"/><circle cx="8" cy="8" r="5.5" stroke="#a09070" stroke-width="1" opacity=".4"/><circle cx="8" cy="8" r="7.5" stroke="#a09070" stroke-width=".7" opacity=".15"/>`;
    document.getElementById('btn-auto-label').textContent = 'AUTO';
  }

  // Analyze
  if (aiMode === 'analyze') {
    btnAnalyze.style.background = 'rgba(245,158,11,.15)'; btnAnalyze.style.borderColor = 'rgba(245,158,11,.33)'; btnAnalyze.style.color = '#f59e0b';
  } else {
    btnAnalyze.style.background = 'transparent'; btnAnalyze.style.borderColor = 'var(--border2)'; btnAnalyze.style.color = 'var(--tx1)';
  }

  // Chat
  if (aiMode === 'chat') {
    btnChat.style.background = 'rgba(129,140,248,.15)'; btnChat.style.borderColor = 'rgba(129,140,248,.33)'; btnChat.style.color = '#818cf8';
  } else {
    btnChat.style.background = 'transparent'; btnChat.style.borderColor = 'var(--border2)'; btnChat.style.color = 'var(--tx1)';
  }
}

// ─── INIT ─────────────────────────────────────────────────────────────────────
renderSidebar();
updateBreadcrumb();
// Hide splash after first source loads
const _origFetch = window.fetchLogs;
switchSource('syslog');
// Splash hides after first successful render (renderLogs / renderSmart / fetchSysmon all call this)
const _origRender = window.renderLogs;
setTimeout(() => { window.__hideSplash && window.__hideSplash(); }, 1200);
</script>
</body>
</html>
"""

@app.get("/")
async def pi_dashboard():
    """Serve the Pi node dashboard UI."""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=_PI_DASHBOARD_HTML)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 7655)))
