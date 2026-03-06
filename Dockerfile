# ── Stage 1: Build React frontend ─────────────────────────────────────────────
FROM node:20-alpine AS builder
WORKDIR /app
COPY package.json .
RUN npm install
COPY index.html .
COPY vite.config.js .
COPY src/    src/
COPY public/ public/
RUN npm run build

# ── Stage 2: Single Python service — does everything ──────────────────────────
# Gets pid:host + all volume mounts in docker-compose → sees real host data
FROM python:3.12-slim
WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        smartmontools \
        util-linux \
        procps \
        curl \
        docker.io \
        systemd \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy built React SPA
COPY --from=builder /app/dist /app/dist

# Single unified backend
COPY main.py .

ENV OLLAMA_HOST=http://host.docker.internal:11434
ENV GOTIFY_HOST=""

EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -f http://localhost:8080/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--log-level", "warning", "--workers", "1"]
