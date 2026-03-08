"""
AXIOM  v1.0  — single-container, all-in-one
─────────────────────────────────────────────
One container does everything:
  • Serves the React SPA from /app/dist
  • Real host metrics via psutil        (container runs with pid: host)
  • Real host logs from /host/log       (volume: /var/log → /host/log)
  • Streams AI analysis via Ollama
  • Server-side Gotify push             (token never hits the browser)
  • Proxies /ollama/* → Ollama host
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

import httpx
import psutil
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse, Response, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── Config ─────────────────────────────────────────────────────────────────────
OLLAMA_HOST  = os.getenv("OLLAMA_HOST",  "http://host.docker.internal:11434").rstrip("/")
GOTIFY_HOST  = os.getenv("GOTIFY_HOST",  "").rstrip("/")
GOTIFY_TOKEN = os.getenv("GOTIFY_TOKEN", "")
OLLAMA_MODEL = "llama3.2:1b"
LOG_ROOT     = Path("/host/log")
STATIC_DIR   = Path("/app/dist")

app = FastAPI(title="AXIOM", version="1.0", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── In-memory sysmon application log ──────────────────────────────────────────
# Circular buffer of the last 500 log lines from sysmon itself (kills, errors, etc.)
_APP_LOG: collections.deque = collections.deque(maxlen=500)

def _app_log(msg: str, level: str = "INFO"):
    entry = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}"
    _APP_LOG.append(entry)
    print(entry, flush=True)   # also goes to docker logs


@app.get("/api/sysmon-logs")
async def get_sysmon_logs():
    """Return sysmon's own application log (kills, errors, startup events)."""
    return {"lines": list(_APP_LOG), "count": len(_APP_LOG)}

_app_log(f"AXIOM v1.0 starting — pid_ns:host uid:{os.getuid()} pid:{os.getpid()}")


# ══════════════════════════════════════════════════════════════════════════════
# HEALTH
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {
        "status":    "ok",
        "version":   "1.0",
        "log_root":  str(LOG_ROOT),
        "log_exists": LOG_ROOT.exists(),
    }


@app.get("/api/metrics")
async def get_metrics():
    """System metrics — mirrors pi_agent /api/metrics so the fleet dashboard works uniformly."""
    cpu      = psutil.cpu_percent(interval=0.3)
    mem      = psutil.virtual_memory()
    swap     = psutil.swap_memory()
    load1, load5, load15 = psutil.getloadavg()
    boot_ts  = psutil.boot_time()
    uptime_s = int(time.time() - boot_ts)
    h, rem   = divmod(uptime_s, 3600)
    m        = rem // 60
    uptime_h = f"{h}h {m}m" if h else f"{m}m"

    freq = None
    try:
        f = psutil.cpu_freq()
        if f:
            freq = {"current_mhz": round(f.current), "max_mhz": round(f.max)}
    except Exception:
        pass

    _REAL = re.compile(r"^(/$|/mnt/|/media/|/boot/|/home/|/data/|/storage/|/opt/)")
    disks = []
    seen  = set()
    # With pid:host, /proc/1/root gives us the host filesystem.
    # Read host mounts from /proc/1/mounts and stat via /proc/1/root/<mp>
    HOST_ROOT = "/proc/1/root"
    try:
        with open("/proc/1/mounts") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 2:
                    continue
                mp = parts[1]
                if not _REAL.match(mp) or mp in seen:
                    continue
                seen.add(mp)
                try:
                    host_path = HOST_ROOT + mp if mp != "/" else HOST_ROOT
                    st = os.statvfs(host_path)
                    total = st.f_frsize * st.f_blocks
                    free  = st.f_frsize * st.f_bavail
                    used  = total - free
                    pct   = round(used / total * 100, 1) if total else 0.0
                    disks.append({
                        "mount":    mp,
                        "total_gb": round(total / 1e9, 1),
                        "used_gb":  round(used  / 1e9, 1),
                        "free_gb":  round(free  / 1e9, 1),
                        "percent":  pct,
                    })
                except Exception:
                    pass
    except Exception:
        pass  # fallback: no disks shown rather than container-only data

    temp_c = None
    try:
        sensors = psutil.sensors_temperatures()
        for key in ("coretemp", "acpitz", "cpu_thermal", "k10temp"):
            if key in sensors and sensors[key]:
                temp_c = round(sensors[key][0].current, 1)
                break
        if temp_c is None:
            for entries in sensors.values():
                if entries:
                    temp_c = round(entries[0].current, 1)
                    break
    except Exception:
        pass

    # Read hostname from host (container hostname is a random ID)
    node = "main"
    try:
        with open("/proc/1/root/etc/hostname") as fh:
            node = fh.read().strip()
    except Exception:
        try:
            import socket
            node = socket.gethostname()
        except Exception:
            pass

    return {
        "node":         node,
        "role":         "main",
        "model":        "AXIOM Main Server",
        "uptime_s":     uptime_s,
        "uptime_human": uptime_h,
        "cpu_percent":  cpu,
        "cpu_count":    psutil.cpu_count(logical=True),
        "load_1":       round(load1, 2),
        "load_5":       round(load5, 2),
        "load_15":      round(load15, 2),
        "memory": {
            "total_gb": round(mem.total / 1e9, 1),
            "used_gb":  round(mem.used  / 1e9, 1),
            "percent":  mem.percent,
        },
        "swap": {
            "total_gb": round(swap.total / 1e9, 1),
            "used_gb":  round(swap.used  / 1e9, 1),
            "percent":  swap.percent,
        },
        "disks":         disks,
        "temperature_c": temp_c,
        "freq":          freq,
        "throttle":      None,
    }


# ══════════════════════════════════════════════════════════════════════════════
# GOTIFY  — server-side push (token never exposed to browser)
# ══════════════════════════════════════════════════════════════════════════════

class GotifyMessage(BaseModel):
    title:    str
    message:  str
    priority: int = 5


async def _gotify_send(title: str, message: str, priority: int = 5) -> dict:
    if not GOTIFY_HOST or not GOTIFY_TOKEN:
        return {"ok": False, "error": "GOTIFY_HOST or GOTIFY_TOKEN not set in .env"}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(
                f"{GOTIFY_HOST}/message",
                headers={"Content-Type": "application/json", "X-Gotify-Key": GOTIFY_TOKEN},
                json={"title": title, "message": message, "priority": priority},
            )
        if r.status_code in (200, 201):
            return {"ok": True}
        if r.status_code == 401:
            return {"ok": False, "error": "Token rejected (401) — check GOTIFY_TOKEN in .env"}
        if r.status_code == 403:
            return {"ok": False, "error": "Forbidden (403) — token lacks send permission"}
        return {"ok": False, "error": f"Gotify returned HTTP {r.status_code}"}
    except httpx.ConnectError:
        return {"ok": False, "error": f"Cannot connect to {GOTIFY_HOST} — is Gotify running?"}
    except httpx.TimeoutException:
        return {"ok": False, "error": f"Timeout connecting to {GOTIFY_HOST}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/gotify/status")
async def gotify_status():
    configured  = bool(GOTIFY_HOST and GOTIFY_TOKEN)
    token_hint  = ("••••" + GOTIFY_TOKEN[-4:]) if len(GOTIFY_TOKEN) >= 4 else ("set" if GOTIFY_TOKEN else "")
    reachable   = None
    reach_error = None
    if GOTIFY_HOST:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{GOTIFY_HOST}/health")
                reachable = r.status_code < 500
        except Exception as e:
            reachable   = False
            reach_error = str(e)
    return {
        "configured":  configured,
        "host":        GOTIFY_HOST or "",
        "token_hint":  token_hint,
        "reachable":   reachable,
        "reach_error": reach_error,
    }


@app.post("/api/gotify/test")
async def gotify_test():
    if not GOTIFY_HOST or not GOTIFY_TOKEN:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "Set GOTIFY_HOST and GOTIFY_TOKEN in .env then rebuild."
        })
    result = await _gotify_send(
        title    = "🟢 AXIOM connected",
        message  = (
            f"AXIOM is connected!\nHost: {GOTIFY_HOST}\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            "You will receive alerts here when errors or warnings are detected."
        ),
        priority = 4,
    )
    return JSONResponse(status_code=200 if result["ok"] else 502, content=result)


@app.post("/api/gotify/send")
async def gotify_send(msg: GotifyMessage):
    result = await _gotify_send(msg.title, msg.message, msg.priority)
    return JSONResponse(status_code=200 if result["ok"] else 502, content=result)


# ══════════════════════════════════════════════════════════════════════════════
# OLLAMA PROXY
# ══════════════════════════════════════════════════════════════════════════════

@app.api_route("/ollama/{path:path}", methods=["GET", "POST", "OPTIONS"])
async def ollama_proxy(path: str, request: Request):
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        })
    target  = f"{OLLAMA_HOST}/{path}"
    body    = await request.body()
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length", "authorization")}

    async def stream():
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(request.method, target, headers=headers,
                                         params=dict(request.query_params), content=body) as resp:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
        except Exception as e:
            yield json.dumps({"error": str(e)}).encode()

    return StreamingResponse(stream(), media_type="application/json",
                             headers={"Access-Control-Allow-Origin": "*"})


# ══════════════════════════════════════════════════════════════════════════════
# LOGS
# ══════════════════════════════════════════════════════════════════════════════

LOG_SOURCES = {
    "kernel": {"label": "Kernel",          "files": ["kern.log", "kern.log.1"],    "desc": "Hardware errors, kernel panics",              "pattern": None},
    "syslog": {"label": "System",          "files": ["syslog", "syslog.1"],        "desc": "General system events, daemons",              "pattern": None},
    "auth":   {"label": "Auth / Security", "files": ["auth.log", "auth.log.1"],    "desc": "SSH logins, sudo, PAM, failed auth attempts", "pattern": None},
    "boot":   {"label": "Boot",            "files": ["boot.log", "dmesg"],         "desc": "Boot sequence and hardware detection",        "pattern": None},
    "docker": {"label": "Docker",          "files": ["syslog"],                    "desc": "Container events",                            "pattern": r"(docker|containerd|container)"},
    "disk":   {"label": "Disk / Storage",  "files": ["syslog", "kern.log"],        "desc": "Disk I/O errors, filesystem events",          "pattern": r"(sd[a-z]|nvme|ata[0-9]|EXT4|XFS|BTRFS|I/O error|blk_|scsi)"},
}

EXTRA_LOG_CANDIDATES = {
    "syslog": ["syslog", "syslog.1", "messages", "messages.1", "system.log"],
    "kernel": ["kern.log", "kern.log.1", "messages", "messages.1"],
    "auth":   ["auth.log", "auth.log.1", "secure", "secure.1"],
    "boot":   ["boot.log", "boot.log.1", "dmesg"],
    "docker": ["syslog", "syslog.1", "messages"],
    "disk":   ["syslog", "syslog.1", "kern.log", "messages"],
}

JOURNAL_DIRS = [
    "/run/log/journal",       # live journal (most current, runtime mount)
    "/host/log/journal",      # host /var/log/journal mounted at /host/log/journal
    "/var/log/journal",       # persistent journal if accessible directly
]


def _filter(lines: list, pattern: Optional[str]) -> list:
    if not pattern:
        return lines
    rx = re.compile(pattern, re.IGNORECASE)
    return [l for l in lines if rx.search(l)]


def _run(cmd: list, timeout: int = 12) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
        return r.stdout
    except Exception:
        return ""


def read_log_tail(filename: str, lines: int, pattern=None) -> list:
    path = LOG_ROOT / filename
    if not path.exists():
        return []
    try:
        fetch  = lines * 4 if pattern else lines
        result = subprocess.run(["tail", "-n", str(fetch), str(path)],
                                capture_output=True, text=True, timeout=10, errors="replace")
        return _filter(result.stdout.splitlines(), pattern)[-lines:]
    except Exception:
        return []


def read_files(log_type: str, lines: int) -> list:
    pattern = LOG_SOURCES[log_type].get("pattern")
    for fname in EXTRA_LOG_CANDIDATES.get(log_type, []):
        chunk = read_log_tail(fname, lines, pattern)
        if chunk:
            return chunk
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


def _read_machine_id() -> Optional[str]:
    """Read host machine-id from the mounted file."""
    try:
        mid = Path("/etc/machine-id").read_text().strip()
        return mid if mid else None
    except Exception:
        return None

_MACHINE_ID = _read_machine_id()


def _journalctl_base(fetch: int, args: Optional[list], jdir: Optional[str]) -> list:
    """Try one journalctl invocation; return lines or []."""
    dir_flag = ["-D", jdir] if jdir else []
    cmd = (
        ["journalctl", "--no-pager", "-q", "--output=short-iso", f"-n{fetch}"]
        + dir_flag
        + (args or [])
    )
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15, errors="replace")
        out = [l for l in r.stdout.splitlines() if l and not l.startswith("--")]
        return out
    except FileNotFoundError:
        return []          # journalctl not installed
    except Exception:
        return []


def read_journal(lines: int, args: Optional[list] = None, pattern=None) -> list:
    fetch = lines * 4 if pattern else lines

    # Collect all existing journal dirs (deduplicated)
    seen_dirs: set = set()
    ordered_dirs: list = []
    for d in JOURNAL_DIRS:
        p = Path(d)
        if p.exists():
            real = str(p.resolve())
            if real not in seen_dirs:
                seen_dirs.add(real)
                ordered_dirs.append(d)

    # Attempts: each known dir first, then no -D flag (uses system default)
    attempts = [d for d in ordered_dirs] + [None]

    for jdir in attempts:
        out = _journalctl_base(fetch, args, jdir)
        if out:
            return _filter(out, pattern)[-lines:]

    return []


def read_docker_logs(lines: int) -> list:
    if Path("/var/run/docker.sock").exists():
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
    for fname in ["syslog", "messages", "daemon.log", "syslog.1"]:
        chunk = read_log_tail(fname, lines * 4, pattern=r"(docker|containerd|container)")
        if chunk:
            return chunk[-lines:]
    return read_journal(lines, ["-u", "docker", "-u", "containerd"])


def read_log_source(log_type: str, lines: int) -> list:
    try:
        pattern = LOG_SOURCES[log_type].get("pattern")

        # ── Docker: try socket first, then journal ────────────────────────────
        if log_type == "docker":
            return read_docker_logs(lines) or read_journal(lines, ["-u", "docker", "-u", "containerd"])

        # ── kernel / boot / disk: dmesg is the best source ───────────────────
        if log_type in ("kernel", "boot", "disk"):
            dmesg_out = read_dmesg(lines, pattern if log_type == "disk" else None)
            if dmesg_out:
                return dmesg_out

        # ── Try traditional log files in /var/log (via /host/log) ────────────
        file_out = read_files(log_type, lines)
        if file_out:
            return file_out

        # ── Fall back to journalctl ───────────────────────────────────────────
        jmap = {
            "kernel": ["-k"],
            "syslog": [],
            "auth":   ["-t", "sudo", "-t", "sshd", "-t", "pam", "-t", "login",
                       "-t", "passwd", "-t", "su", "-t", "polkit"],
            "boot":   ["-b"],
            # disk: try kernel log filtered, then unfiltered kernel
            "disk":   ["-k"],
        }
        journal_out = read_journal(lines, jmap.get(log_type, []), pattern)
        if journal_out:
            return journal_out

        # ── Last resort for disk: journalctl without pattern filter ───────────
        if log_type == "disk":
            journal_out = read_journal(lines, ["-k"])
            if journal_out:
                return [l for l in journal_out if re.search(pattern, l, re.IGNORECASE)] or journal_out

        # ── Diagnostic message — tell user WHY it failed ──────────────────────
        jdirs  = [d for d in JOURNAL_DIRS if Path(d).exists()]
        files  = list((LOG_ROOT).glob("*")) if LOG_ROOT.exists() else []
        fnames = [f.name for f in sorted(files)[:12]]
        jcheck = _run(["journalctl", "--no-pager", "-q", "--output=short-iso", "-n1"])
        return [
            f"[No '{log_type}' logs found]",
            f"  /var/log mounted at {LOG_ROOT}: {LOG_ROOT.exists()}",
            f"  Files in {LOG_ROOT}: {fnames or 'none'}",
            f"  Journal dirs found: {jdirs or 'none'}",
            f"  journalctl test: {'OK — ' + jcheck[:60].strip() if jcheck.strip() else 'no output / not installed'}",
            f"  Tip: if /var/log/syslog is missing, your system uses journald only.",
            f"  The journal volume mount in docker-compose.yml provides read access.",
        ]
    except Exception as e:
        return [f"[Internal error reading {log_type}: {e}]"]


def _smartctl_run(drive: str, extra_flags: list = None) -> subprocess.CompletedProcess:
    """Run smartctl with optional transport flags (e.g. ['-d', 'sat'])."""
    cmd = ["smartctl", "-H", "-A"] + (extra_flags or []) + [drive]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10, errors="replace")


def _parse_smart_output(drive: str, result: subprocess.CompletedProcess) -> dict:
    """Parse smartctl output into a structured entry dict."""
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
                if any(k in name for k in ["Reallocated", "Pending", "Uncorrectable",
                                            "Temperature", "Power_On", "Wear",
                                            "Erase_Fail", "Program_Fail"]):
                    attrs.append(f"  {name}: {raw}")
    return {
        "drive":  drive,
        "health": health_line.strip() or "Unknown",
        "attrs":  attrs[:12],
        "raw":    output[:3000],
    }


# Transport types to try in order for drives that fail the default probe
_SAT_TRANSPORTS = ["-d sat", "-d sat,12", "-d usb", "-d auto"]


def get_smart_data() -> dict:
    drives = []
    try:
        result = subprocess.run(["lsblk", "-dno", "NAME,TYPE"],
                                capture_output=True, text=True, timeout=5, errors="replace")
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == "disk":
                drives.append(f"/dev/{parts[0]}")
    except Exception:
        drives = ["/dev/sda"]

    smart_results = []
    unreadable    = []

    for drive in drives:
        try:
            # First attempt: default (works for native SATA/NVMe)
            result = _smartctl_run(drive)
            output = result.stdout + result.stderr

            # Retry with SAT/USB transport flags if:
            #   - Permission denied (USB bridge device)
            #   - No health line found (transport not auto-detected)
            #   - returncode 2 (open device failed)
            needs_retry = (
                "Permission denied" in output or
                "failed:" in output or
                result.returncode == 2 or
                "SMART overall-health" not in output
            )

            if needs_retry:
                for transport in _SAT_TRANSPORTS:
                    flags = transport.split()
                    retry = _smartctl_run(drive, flags)
                    retry_out = retry.stdout + retry.stderr
                    if "SMART overall-health" in retry_out and "Permission denied" not in retry_out:
                        result = retry
                        break

            entry = _parse_smart_output(drive, result)
            output = result.stdout + result.stderr

            # Classify: real drive vs unsupported/virtual
            # returncode 0 = OK, 4 = warning threshold exceeded (still a real drive)
            is_virtual_or_unsupported = (
                not entry["health"] or
                entry["health"] == "Unknown" or
                "not supported" in output.lower() or
                "SMART support is: Unavailable" in output or
                (result.returncode not in (0, 4) and "SMART overall-health" not in output)
            )

            if is_virtual_or_unsupported:
                # Give a more informative reason
                if "zram" in drive:
                    entry["health"] = "Virtual (zram swap)"
                elif "Permission denied" in output and "SMART overall-health" not in output:
                    entry["health"] = "Permission denied — try running with --privileged"
                unreadable.append(entry)
            else:
                smart_results.append(entry)

        except FileNotFoundError:
            unreadable.append({"drive": drive, "health": "smartctl not installed",
                               "attrs": [], "raw": ""})
        except Exception as e:
            unreadable.append({"drive": drive, "health": f"Error: {e}",
                               "attrs": [], "raw": str(e)})

    return {"drives": smart_results, "unreadable": unreadable}


def build_prompt(log_type: str, log_lines: list) -> str:
    source = LOG_SOURCES.get(log_type) or {"label": log_type.upper(), "desc": log_type}
    sample = "\n".join(log_lines[-80:])
    return (
        f"You are a senior Linux sysadmin AI analyzing {source.get('label', log_type)} logs.\n\n"
        f"Log source: {source.get('desc', '')}\n"
        f"Lines analyzed: {len(log_lines)}\n"
        f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"LOG SAMPLE:\n---\n{sample}\n---\n\n"
        "Respond in EXACTLY this format, nothing else:\n\n"
        "🔍 SUMMARY: <one sentence — what is happening in these logs>\n"
        "⚠️  ISSUES: <specific errors or anomalies found — or \"None detected\">\n"
        "🔧 ACTION: <one concrete command or fix — or \"No action needed\">\n"
        "📊 SEVERITY: <NORMAL | WARNING | CRITICAL> — <one-sentence reason>"
    )


@app.get("/api/sources")
async def get_sources():
    result = {}
    for key, src in LOG_SOURCES.items():
        exists = any((LOG_ROOT / f).exists() for f in src["files"])
        result[key] = {"label": src["label"], "desc": src["desc"],
                       "available": exists or key in ("disk", "docker", "boot")}
    result["smart"] = {"label": "Drive SMART", "desc": "Hardware drive health via smartctl",
                       "available": True}
    return result


@app.get("/api/logs/{log_type}")
async def get_logs(log_type: str, lines: int = Query(default=100, ge=10, le=2000)):
    if log_type == "smart":
        return get_smart_data()
    if log_type not in LOG_SOURCES:
        raise HTTPException(404, f"Unknown log type: {log_type}")
    source  = LOG_SOURCES[log_type]
    entries = read_log_source(log_type, lines)
    if not entries:
        entries = [f"[No '{log_type}' logs found — /var/log is mounted at /host/log: {LOG_ROOT.exists()}]"]
    return {
        "type": log_type, "label": source["label"],
        "lines": len(entries), "requested": lines,
        "entries": entries, "fetched_at": datetime.now().isoformat(),
    }


@app.get("/api/analyze/{log_type}")
async def analyze_logs(log_type: str, lines: int = Query(default=100, ge=10, le=500)):
    if log_type == "smart":
        smart = get_smart_data()
        log_lines = []
        for drive in smart["drives"]:
            log_lines.append(f"=== {drive['drive']} — {drive['health']} ===")
            log_lines.extend(drive["attrs"])
    elif log_type in LOG_SOURCES:
        log_lines = read_log_source(log_type, lines)
    else:
        raise HTTPException(404, f"Unknown log type: {log_type}")

    if not log_lines:
        log_lines = ["[No log data available]"]

    prompt = build_prompt(log_type, log_lines)

    async def stream_ollama():
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                async with client.stream(
                    "POST", f"{OLLAMA_HOST}/api/generate",
                    json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": True,
                          "options": {"temperature": 0.1, "num_predict": 350}},
                ) as resp:
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

    return StreamingResponse(stream_ollama(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════
# ASK AI  — interactive Q&A about log context
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# ASK AI  — interactive Q&A about a log source
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/ask")
async def ask_ai(
    source:   str = Query(default="syslog"),
    question: str = Query(default="What is happening in these logs?"),
    context:  str = Query(default=""),
):
    """Stream an Ollama answer to a user question given recent log context."""
    prompt = (
        f"You are a senior Linux sysadmin and security expert embedded in a log viewer called AXIOM.\n"
        f"The operator is viewing the '{source}' log source and asks:\n\n"
        f"QUESTION: {question}\n\n"
        f"RECENT LOG CONTEXT (last ~80 lines):\n"
        f"```\n{context[:4500]}\n```\n\n"
        "Answer the question directly and practically:\n"
        "- Give the root cause if it is an error\n"
        "- Provide concrete shell commands where appropriate\n"
        "- Flag security implications if relevant\n"
        "- Be concise — under 200 words unless complexity demands more\n"
        "- Use plain text, no markdown headers\n"
    )

    async def stream_ask():
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST", f"{OLLAMA_HOST}/api/generate",
                    json={
                        "model":   OLLAMA_MODEL,
                        "prompt":  prompt,
                        "stream":  True,
                        "options": {"temperature": 0.25, "num_predict": 450},
                    },
                ) as resp:
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

    return StreamingResponse(stream_ask(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# STATIC FILE SERVING  — React SPA (must be last, catches everything else)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    if not STATIC_DIR.exists():
        return Response("Frontend not built. Run: docker compose build", status_code=503)
    candidate = STATIC_DIR / full_path
    if candidate.is_file():
        return FileResponse(str(candidate))
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return Response("Not found", status_code=404)

