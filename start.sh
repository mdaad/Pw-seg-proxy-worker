#!/data/data/com.termux/files/usr/bin/bash

# ===== CONFIG =====
APP_MODULE="server:app"   # agar file scrap.py ho to "scrap:app" kar dena
HOST="0.0.0.0"
PORT="5000"
WORKERS="1"
THREADS="8"
TIMEOUT="120"
KEEPALIVE="10"
MAXREQ="200"
JITTER="30"

# ===== TERMUX WAKE LOCK =====
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock

# ===== KILL OLD PROCESSES =====
pkill -f "gunicorn.*${APP_MODULE}" 2>/dev/null
pkill -f "python3 server.py" 2>/dev/null
pkill -f "python3 scrap.py" 2>/dev/null

# ===== SMALL DELAY =====
sleep 1

# ===== START SILENTLY =====
nohup gunicorn \
  -w "${WORKERS}" \
  --threads "${THREADS}" \
  -k gthread \
  -b "${HOST}:${PORT}" \
  --timeout "${TIMEOUT}" \
  --keep-alive "${KEEPALIVE}" \
  --max-requests "${MAXREQ}" \
  --max-requests-jitter "${JITTER}" \
  "${APP_MODULE}" >/dev/null 2>&1 &

sleep 2

# ===== CHECK =====
if pgrep -f "gunicorn.*${APP_MODULE}" >/dev/null; then
  echo "✅ Proxy started on ${HOST}:${PORT}"
else
  echo "❌ Start failed"
fi
