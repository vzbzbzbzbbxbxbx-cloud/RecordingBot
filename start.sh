#!/usr/bin/env bash
set -euo pipefail

echo "=== RippingBot Ultimate starting ==="

: "${BOT_TOKEN:?BOT_TOKEN is not set}"
: "${OWNER_ID:?OWNER_ID is not set}"
: "${MONGO_URI:?MONGO_URI is not set}"

mkdir -p bot/data/downloads bot/data/tmp bot/data/logs || true

if command -v ffmpeg >/dev/null 2>&1; then
  echo "[OK] ffmpeg found"
else
  echo "[WARN] ffmpeg not found (recording will fail)"
fi

if command -v ffprobe >/dev/null 2>&1; then
  echo "[OK] ffprobe found"
else
  echo "[WARN] ffprobe not found (duration detection best-effort)"
fi

exec python -m bot.main
