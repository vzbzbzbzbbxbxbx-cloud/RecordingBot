# RippingBot Ultimate — Stream Recorder (Telegram)

**Author / Branding:** DoraemonBro © 2026  
A modular Telegram stream-recording bot built for stability, queue control, and clean UX.

> ⚠️ **Use responsibly:** Only record streams you are authorized to access and archive.  
> This project is designed for legitimate recording/archiving workflows.

---

## Features

### Core
- **No 600MB limit** (removed)
- **Auto-splitting at 2000MB (2GB)** before upload
- **No Mega upload methods** (fully removed)
- **Up to 3 concurrent recordings** (hard limit enforced)
- **Robust queue system**
- **FFmpeg auto-reconnect** for unstable streams
- **Live progress editing** (record + upload):
  - progress bar
  - current speed
  - ETA

### Database (MongoDB)
MongoDB is used to store and track:
- `/playlist` (user playlists, channels)
- `/auth` & `/rm` (premium)
- `/trial` (trial credits)
- Daily usage tracking & resets
- `/proxy` configuration
- `/schedule` jobs

### Access Control
- **Owner (ID: 8368957390):** **no limitations** (can run anywhere)
- **Everyone else:** can use **ONLY** in group: **-5170978969**

### Commands
- `/playlist` — add playlist (reply to file or send URL)
- `/channel` — list channels from stored playlist
- `/record` — record by link or by playlist channel name
- `/schedule` — schedule recordings
- `/cancel` — cancel your active/queued task
- `/tasks` — view running/queued tasks
- `/status` (or `/Status`) — user time & limits
- `/stats` (or `/Stats`) — system stats
- `/proxy` — owner-only proxy manager with inline buttons
- `/hot`, `/cold`, `/dark` — legacy theme switching (fully supported)

### UI / UX
- Inline buttons for:
  - 📽️ Quality selection (from master playlist)
  - 🎶 Audio selection
  - 🎶 **All** audio option (select all tracks)
  - ❌ Cancel
- Uploads are sent as **playable media** with **thumbnail**
  - bot tries `sendVideo`
  - if `.mkv` is rejected → auto-remux to `.mp4` then uploads

---

## Requirements

- **Python:** 3.10+ recommended
- **FFmpeg:** must be installed and available in PATH
  - `ffmpeg` and `ffprobe` required
- **MongoDB:** Atlas or self-hosted

---

## Installation

### 1) Install dependencies
```bash
pip install -r bot/requirements.txt

## Configure environment
Copy env.example → .env and fill values:
Required
BOT_TOKEN=...
MONGO_URI=...
MONGO_DB_NAME=...
Access Control
OWNER_ID=8368957390
GROUP_ID=-5170978969
Splitting
PART_MAX_MB=2000
Concurrency
MAX_CONCURRENT=3
Daily Reset
DAILY_RESET_TIME=23:59
(Reset uses Asia/Dhaka logic by default in code/config.)

## Run the bot
python -m bot.main

## Usage Guide
/playlist
Add a playlist in two ways:
Reply to a playlist file (m3u / m3u8)
Send a playlist URL (HTTP/HTTPS)
Examples:
Reply to a message containing a file → type:
/playlist
Send URL:
/playlist https://example.com/mylist.m3u
Auto-refresh
Global refresh runs every 5 minutes
Active recording also re-resolves playlist/channel URLs between parts (best-effort)
/channel
Lists available channels from your stored playlist.
Example:
/channel

Method 1: record direct link
/record <m3u8_link> <duration> <filename>
Method 2: record by playlist channel name
/record "Channel Name" 01:00:00 MyChannelRecording
Quality + Audio selection
If the source is a master playlist, the bot will show 📽️ quality buttons.
Then it shows 🎶 audio track buttons (+ 🎶 All).

Schedules a recording.
/schedule <link OR "channel name"> <time> <file_name> [duration]
Time formats supported:
HH:MM (today)
YYYY-MM-DD HH:MM
Examples:
/schedule "Star Plus HD" 2026-02-28 10:00 MorningEpisode 00:30:00
Cancels your active recording (and any queued jobs under your user).
/cancel ❌
/status (or /Status)
Shows your tier + today’s usage + remaining limits.
Rules:
Premium users: 6 hours/day
Trial users: 3 hours/day
Owner: no limits
/stats (or /Stats)
Shows system stats:
CPU / RAM
Active tasks
Queue length
Disk / runtime info (if enabled)
/auth & /rm (admin/owner workflows)
Use by replying to a user's message.
Grant premium:
/auth 1d
Remove premium:
/rm
/trial
Use by replying to a user’s message:
/trial 2/3/45
Trial credits are tracked in MongoDB.
/proxy (Owner only)
Add a proxy:
/proxy http://user:pass@host🏅
Remove proxy:
Run /proxy with no args → bot shows inline buttons to remove.
Proxy is saved to MongoDB and applied to fetch/upload modules that support it.

##File Splitting & Upload Rules
If output file exceeds 2000MB, it is split into parts:
Name.part01.mp4
Name.part02.mp4
...
Each part is uploaded as playable video with thumbnail.
If source produces .mkv:
bot tries upload as video
if Telegram rejects → bot remuxes to .mp4 then uploads


## Folder Structure (high level)
bot/
  main.py                # bot entrypoint
  config.py              # config/env loader
  access.py              # owner/group access rules
  db.py                  # MongoDB client + collections
  limits.py              # premium/trial usage logic
  task_manager.py        # queue + concurrency (max 3)
  ui.py                  # all UI builders (progress bars, layouts)
  messages.py            # all replies/errors/success templates (extensible)
  buttons.py             # inline button builders (quality/audio/proxy)
  handlers/
    playlist.py          # /playlist, /channel
    record.py            # /record logic
    schedule.py          # /schedule logic
    admin.py             # /auth /rm /trial
    proxy.py             # /proxy
    theme.py             # /hot /cold /dark
  utils/
    chunk_pipeline.py    # ffmpeg record, split, retry, reconnect
    uploader.py          # Telegram upload with progress + ETA
    m3u.py               # playlist parser + channel resolver
    hls_master.py        # quality/audio parsing for master playlists
    ffprobe.py           # duration/streams detection helpers

## MongoDB Collections (typical)
users
user_id
premium_until
trial_credits
used_seconds
usage_date
playlists
user_id
playlist_url OR raw_text
channels[]
updated_at
proxy
proxy_url
enabled
scheduled_jobs
job_id, user_id, run_at, source, duration, filename, status

## Upload fails for MKV
Bot will attempt .mp4 remux automatically. If still failing:
Check file size per part (2GB cap)
Check bot API limits / Telegram restrictions
Mongo errors
Verify:
MONGO_URI is correct
IP whitelist is set (Atlas)
DB user has permissions

## Credits
Built and branded by @DoraemonBro.
