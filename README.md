# RippingBot Ultimate (2GB Split + Queue + Mongo)

A Telegram stream-recording bot (python-telegram-bot v20+) with:

- ✅ **2GB auto-splitting** (records in ~2GB parts, uploads each part)
- ✅ **No 600MB limit**, **no MEGA**
- ✅ **Hard concurrency cap: 3 active recordings**
- ✅ **Robust queue**
- ✅ **Auto-reconnect recording**
- ✅ **Real-time progress (upload + recording)**
- ✅ **MongoDB storage** for:
  - /playlist
  - /auth (premium)
  - /trial (attempts)
  - usage limits + proxy + schedules
- ✅ Uploads as **playable video with thumbnail** (sendVideo). If Telegram rejects MKV, it auto-remuxes to MP4 for upload.

> Use only streams you own or have permission to record.

## Setup

1) Install dependencies:
```bash
pip install -r bot/requirements.txt
```

2) Export environment variables (or create `.env` and source it):
- See `env.example`

3) Run:
```bash
python -m bot.main
```

## Access rules

- **Owner** (`OWNER_ID`) = **no limitations** (can use anywhere, unlimited daily time)
- **Everyone else** = only inside the allowed group (`GROUP_ID`)

## Commands

- `/playlist` (reply to playlist file or URL)  
- `/channel`  
- `/record <m3u8_link> <HH:MM:SS> <filename>`  
- `/record "Channel name from playlist" 00:00:00 file_name`  
- `/schedule <link|"channel"> <time> <filename> [duration]`  
  - time: `HH:MM` or `YYYY-MM-DD HH:MM`
  - duration default = 1 hour if omitted
- `/cancel`  
- `/tasks`  
- `/status`  
- `/stats`  
- `/proxy` (owner)  
- `/auth` (owner, reply)  
- `/rm` (owner, reply)  
- `/trial` (owner, reply)  
- `/hot /cold /dark` (themes)

## Notes

- Playlist URL auto-refresh runs every 5 minutes (configurable).
- Active recordings attempt more frequent refresh (configurable).
