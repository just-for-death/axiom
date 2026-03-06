# AXIOM — Log Server

> Real-time Linux log viewer with AI-powered analysis, S.M.A.R.T drive health, and push notifications. Runs as a single Docker container on your home server.

[![Docker Hub](https://img.shields.io/docker/pulls/justxforxdocker/axiom?style=flat-square&logo=docker&label=Docker%20Hub)](https://hub.docker.com/r/justxforxdocker/axiom)
[![Image Size](https://img.shields.io/docker/image-size/justxforxdocker/axiom/latest?style=flat-square)](https://hub.docker.com/r/justxforxdocker/axiom)

---

## Features

- **Live log sources** — System, Kernel, Auth, Docker, Disk, Boot logs via `journalctl` and `/var/log`
- **AI Analysis** — One-click log analysis streamed from a local [Ollama](https://ollama.ai) instance
- **Ask AI** — Chat interface to ask questions about your logs in plain English
- **S.M.A.R.T Drive Health** — Full attribute tables for all drives including USB/SAT bridge devices
- **Gotify push notifications** — Server-side alerts, token never exposed to the browser
- **Warm amber terminal UI** — Dark carbon theme, Space Mono font, collapsible sidebar, mobile-friendly
- **PWA** — Installable on iOS, Android, and desktop

---

## Quick Start

**Prerequisites:** Docker + Docker Compose, [Ollama](https://ollama.ai) running on the host

### Option A — Prebuilt image (recommended)

```bash
git clone https://github.com/YOUR_USERNAME/axiom.git
cd axiom
cp .env.example .env
docker compose -f docker-compose.prebuilt.yml up -d
```

### Option B — Build from source

```bash
git clone https://github.com/YOUR_USERNAME/axiom.git
cd axiom
cp .env.example .env
docker compose up -d --build
```

> Build takes ~2 minutes (installs system packages + compiles React frontend).

Open **http://localhost:7654**

---

## Configuration

Edit `.env` before starting:

```env
# Port to expose on the host
AXIOM_PORT=7654

# Ollama instance (host.docker.internal resolves to Docker host)
OLLAMA_HOST=http://host.docker.internal:11434

# Gotify push notifications (optional — leave blank to disable)
GOTIFY_HOST=
GOTIFY_TOKEN=
```

---

## Architecture

Single container does everything:

```
┌─────────────────────────────────────────────────┐
│  axiom container (privileged, pid: host)        │
│                                                 │
│  ┌─────────────┐   ┌──────────────────────────┐ │
│  │  React SPA  │   │   FastAPI backend        │ │
│  │  (served    │   │                          │ │
│  │  from dist) │   │  • /api/logs/*           │ │
│  └─────────────┘   │  • /api/analyze/*  (SSE) │ │
│                    │  • /api/ask         (SSE) │ │
│                    │  • /api/gotify/*         │ │
│                    │  • /ollama/* (proxy)     │ │
│                    └──────────────────────────┘ │
│                                                 │
│  Volume mounts (read-only):                     │
│    /var/log          → /host/log                │
│    /run/log/journal  → /run/log/journal         │
│    /var/log/journal  → /var/log/journal         │
│    /etc/machine-id   → /etc/machine-id          │
│    /var/run/docker.sock                         │
│    /dev, /sys                                   │
└─────────────────────────────────────────────────┘
```

A second lightweight container (`axiom-log`) polls the internal app log every 5 seconds and persists it to a named volume at `/var/log/axiom/axiom.log`.

---

## Log Sources

| Source | Where it reads |
|--------|----------------|
| System | `/var/log/syslog` → journalctl fallback |
| Kernel | `dmesg -T` → `/var/log/kern.log` → journalctl `-k` |
| Auth | `/var/log/auth.log` → journalctl `-t sshd -t sudo -t pam` |
| Docker | Docker socket (`docker logs`) → journalctl |
| Disk | `dmesg` filtered for block devices → journalctl `-k` |
| Boot | `dmesg -T` → journalctl `-b` |
| S.M.A.R.T | `smartctl` with SAT/USB transport auto-retry |

Works on both traditional syslog distros and modern journald-only setups (Ubuntu 22.04+, Arch, etc.).

---

## S.M.A.R.T Notes

- Requires `privileged: true` in docker-compose (already set)
- USB/SAT bridge drives (e.g. external SSDs) are automatically retried with `-d sat`, `-d sat,12`, `-d usb`, `-d auto`
- Virtual devices (zram, loop) are shown separately under "Unsupported / Virtual"

---

## Gotify Notifications

1. Set `GOTIFY_HOST` and `GOTIFY_TOKEN` in `.env`
2. Rebuild: `docker compose up -d --build`
3. Open AXIOM → settings area (or hit `/api/gotify/test`) to send a test notification

The token is kept server-side and never sent to the browser.

---

## Development

```bash
# Start the backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8080

# Start the frontend (separate terminal)
npm install
npm run dev   # http://localhost:3000 — proxies /api to :8080
```

---

## Tech Stack

| Layer | Tech |
|-------|------|
| Frontend | React 18, Vite, vanilla CSS-in-JS |
| Backend | FastAPI, uvicorn, httpx |
| AI | Ollama (`llama3.2:1b` default, configurable) |
| Container | Python 3.12-slim + Node 20-alpine (multi-stage build) |
| Fonts | Space Mono, Syne (Google Fonts) |

---

## License

MIT
