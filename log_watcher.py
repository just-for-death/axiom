#!/usr/bin/env python3
"""
sysmon-log: polls /api/sysmon-logs every 5s and writes to a persistent log file.
Run inside the sysmon-log container.
"""

import urllib.request
import json
import time
import os
from datetime import datetime

LOG_PATH = "/var/log/axiom/axiom.log"
SYSMON_URL = "http://axiom:8080"
os.makedirs("/var/log/axiom", exist_ok=True)


def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def write(msg):
    line = f"{ts()} {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


write("[axiom-log] starting up")

# Wait for sysmon to be healthy
while True:
    try:
        urllib.request.urlopen(f"{SYSMON_URL}/health", timeout=3)  # nosec B310
        write("[axiom-log] sysmon is up, starting log collection")
        break
    except Exception:
        print(f"{ts()} [axiom-log] waiting for axiom...", flush=True)
        time.sleep(3)

seen = 0
write(f"[axiom-log] polling {SYSMON_URL}/api/sysmon-logs every 5s → {LOG_PATH}")

while True:
    try:
        resp = urllib.request.urlopen(f"{SYSMON_URL}/api/sysmon-logs", timeout=5)  # nosec B310
        data = json.loads(resp.read())
        lines = data.get("lines", [])
        total = data.get("count", 0)
        
        if seen == 0:
            new_lines = lines
        elif total > seen:
            new_count = total - seen
            new_lines = lines[-new_count:] if new_count <= len(lines) else lines
        else:
            new_lines = []
            
        for line in new_lines:
            write(f"[axiom] {line}")
        seen = total
    except Exception as e:
        write(f"[axiom-log] poll error: {e}")
    time.sleep(5)
