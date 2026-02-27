# bot/config.py
"""
Configuration for the Stream Recording Telegram Bot.

This bot is designed for recording streams you have the rights/permission to record.

Key points:
- 2GB chunk splitting before upload
- Hard global concurrency cap (3 active tasks)
- MongoDB-backed playlist/auth/trial/usage/proxy storage
- Owner (OWNER_ID) has **no limitations** (no access restriction, no daily limits).
- Everyone else can use the bot ONLY in GROUP_ID.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# -------------------------
# Bot identity / access
# -------------------------
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()

OWNER_ID: int = int(os.getenv("OWNER_ID", "8368957390"))
GROUP_ID: int = int(os.getenv("GROUP_ID", "-5170978969"))

# -------------------------
# Storage paths
# -------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).resolve()
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", str(DATA_DIR / "downloads"))).resolve()
TMP_DIR = Path(os.getenv("TMP_DIR", str(DATA_DIR / "tmp"))).resolve()
LOG_DIR = Path(os.getenv("LOG_DIR", str(DATA_DIR / "logs"))).resolve()

for _p in (DATA_DIR, DOWNLOAD_DIR, TMP_DIR, LOG_DIR):
    _p.mkdir(parents=True, exist_ok=True)

# -------------------------
# MongoDB
# -------------------------
USE_MONGO: bool = os.getenv("USE_MONGO", "1").strip() not in ("0", "false", "False")
MONGO_URI: str = os.getenv("MONGO_URI", "").strip()
MONGO_DB_NAME: str = os.getenv("MONGO_DB_NAME", "rippingbot").strip()

# Collections
COL_USERS = "users"
COL_PLAYLISTS = "playlists"
COL_SETTINGS = "settings"
COL_USAGE = "usage"
COL_SCHEDULES = "schedules"

# -------------------------
# Limits & resets (non-owner)
# -------------------------
DAILY_RESET_TZ: str = os.getenv("DAILY_RESET_TZ", "Asia/Dhaka")
# Reset at 23:59 local time (what most users mean by "11:59" daily reset).
DAILY_RESET_HOUR: int = int(os.getenv("DAILY_RESET_HOUR", "23"))
DAILY_RESET_MINUTE: int = int(os.getenv("DAILY_RESET_MINUTE", "59"))

PREMIUM_DAILY_LIMIT_SEC: int = int(os.getenv("PREMIUM_DAILY_LIMIT_SEC", str(6 * 3600)))  # 6 hrs
TRIAL_DAILY_LIMIT_SEC: int = int(os.getenv("TRIAL_DAILY_LIMIT_SEC", str(3 * 3600)))      # 3 hrs

# Trial credits are recording attempts (count-based), not time.
# /trial 1 or 2 or 3 sets this.
DEFAULT_TRIAL_CREDITS: int = int(os.getenv("DEFAULT_TRIAL_CREDITS", "0"))

# -------------------------
# Concurrency / queue
# -------------------------
GLOBAL_MAX_CONCURRENT: int = int(os.getenv("GLOBAL_MAX_CONCURRENT", "3"))
PER_USER_MAX_ACTIVE: int = int(os.getenv("PER_USER_MAX_ACTIVE", "1"))

# -------------------------
# Chunking / upload
# -------------------------
PART_MAX_MB: int = int(os.getenv("PART_MAX_MB", "2000"))  # Split at 2GB
PART_MAX_BYTES: int = PART_MAX_MB * 1024 * 1024

# Prefer MKV output (good for multi-audio); if Telegram rejects as video, we remux to MP4 for upload.
OUTPUT_CONTAINER: str = os.getenv("OUTPUT_CONTAINER", "mkv").strip().lower()

# -------------------------
# Playlist refresh
# -------------------------
# Refresh URL-based playlists globally (every 5 minutes).
PLAYLIST_REFRESH_SEC: int = int(os.getenv("PLAYLIST_REFRESH_SEC", "300"))

# For active recordings, try more often (best effort).
ACTIVE_PLAYLIST_REFRESH_SEC: int = int(os.getenv("ACTIVE_PLAYLIST_REFRESH_SEC", "60"))

# -------------------------
# Progress
# -------------------------
PROGRESS_EDIT_EVERY_SEC: float = float(os.getenv("PROGRESS_EDIT_EVERY_SEC", "2.0"))

# -------------------------
# FFmpeg tools
# -------------------------
FFMPEG_BIN: str = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN: str = os.getenv("FFPROBE_BIN", "ffprobe")

# -------------------------
# Themes
# -------------------------
DEFAULT_THEME: str = os.getenv("DEFAULT_THEME", "cold").strip().lower()

# Version
BOT_VERSION: str = os.getenv("BOT_VERSION", "Ultimate-2GB-Queue-v1")
