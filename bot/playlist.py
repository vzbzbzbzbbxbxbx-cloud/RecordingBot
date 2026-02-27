# bot/playlist.py
from __future__ import annotations

import asyncio
import datetime as _dt
import re
from typing import Dict, List, Any, Optional, Tuple
from urllib.parse import urljoin

import aiohttp
from telegram import Bot

from .db import DB
from .config import PLAYLIST_REFRESH_SEC, ACTIVE_PLAYLIST_REFRESH_SEC

_EXTINF_RE = re.compile(r"#EXTINF:(?P<dur>-?\d+)\s*(?P<attrs>[^,]*)\s*,\s*(?P<name>.*)$", re.IGNORECASE)
_ATTR_RE = re.compile(r'(\w[\w-]*)="([^"]*)"')

def parse_m3u(text: str, base_url: str | None = None) -> List[Dict[str, Any]]:
    """
    Parse M3U/M3U8 into channels with optional headers.
    Supports common IPTV-style tags and VLC opts.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    channels: List[Dict[str, Any]] = []
    pending_headers: Dict[str, str] = {}
    pending_meta: Dict[str, Any] = {}

    def flush_pending():
        nonlocal pending_headers, pending_meta
        pending_headers = {}
        pending_meta = {}

    i = 0
    while i < len(lines):
        ln = lines[i]

        # VLC / IPTV header hints
        if ln.startswith("#EXTVLCOPT:"):
            # e.g. #EXTVLCOPT:http-user-agent=...
            k_v = ln[len("#EXTVLCOPT:"):]
            if "=" in k_v:
                k, v = k_v.split("=", 1)
                k = k.strip().lower()
                v = v.strip()
                if k in ("http-user-agent", "user-agent"):
                    pending_headers["User-Agent"] = v
                elif k in ("http-referrer", "referrer", "referer"):
                    pending_headers["Referer"] = v
                elif k in ("http-cookie", "cookie"):
                    pending_headers["Cookie"] = v
            i += 1
            continue

        if ln.startswith("#EXTHTTP:") or ln.startswith("#EXT-X-HEADER:"):
            # Non-standard header line; best-effort
            # #EXTHTTP:header=User-Agent: Foo
            payload = ln.split(":", 1)[1]
            if "=" in payload:
                _, hv = payload.split("=", 1)
            else:
                hv = payload
            if ":" in hv:
                hk, hvv = hv.split(":", 1)
                pending_headers[hk.strip()] = hvv.strip()
            i += 1
            continue

        m = _EXTINF_RE.match(ln)
        if m:
            name = m.group("name").strip()
            attrs = m.group("attrs") or ""
            attr_map = {k: v for k, v in _ATTR_RE.findall(attrs)}
            group = attr_map.get("group-title") or attr_map.get("group") or None
            logo = attr_map.get("tvg-logo") or attr_map.get("logo") or None
            tvg_name = attr_map.get("tvg-name") or None

            pending_meta = {
                "name": tvg_name or name,
                "group": group,
                "logo": logo,
            }

            # next non-comment line should be URL
            j = i + 1
            while j < len(lines) and lines[j].startswith("#"):
                j += 1
            if j < len(lines):
                url = lines[j].strip()
                if base_url and not url.lower().startswith(("http://", "https://")):
                    url = urljoin(base_url, url)
                channels.append({
                    "name": pending_meta.get("name") or name,
                    "url": url,
                    "headers": dict(pending_headers) if pending_headers else {},
                    "group": pending_meta.get("group"),
                    "logo": pending_meta.get("logo"),
                })
                flush_pending()
                i = j + 1
                continue

        i += 1

    # Filter nonsense
    channels = [c for c in channels if c.get("name") and c.get("url")]
    return channels

async def fetch_text(url: str, headers: Dict[str, str] | None = None, proxy: str | None = None, timeout: int = 20) -> Tuple[str, Dict[str, str]]:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.get(url, headers=headers, proxy=proxy) as resp:
            resp.raise_for_status()
            text = await resp.text(errors="ignore")
            meta = {
                "etag": resp.headers.get("ETag", ""),
                "last_modified": resp.headers.get("Last-Modified", ""),
            }
            return text, meta

def _norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

async def save_playlist_from_url(db: DB, user_id: int, url: str, proxy: str | None = None) -> int:
    text, meta = await fetch_text(url, headers=None, proxy=proxy)
    channels = parse_m3u(text, base_url=url)
    await db.set_playlist(user_id, {
        "user_id": user_id,
        "source_type": "url",
        "source": url,
        "channels": channels,
        "etag": meta.get("etag") or "",
        "last_modified": meta.get("last_modified") or "",
        "last_refreshed_at": _dt.datetime.utcnow(),
    })
    return len(channels)

async def save_playlist_from_file(db: DB, bot: Bot, user_id: int, file_id: str) -> int:
    tg_file = await bot.get_file(file_id)
    content = await tg_file.download_as_bytearray()
    text = content.decode("utf-8", errors="ignore")
    channels = parse_m3u(text, base_url=None)
    await db.set_playlist(user_id, {
        "user_id": user_id,
        "source_type": "file",
        "source": file_id,
        "channels": channels,
        "last_refreshed_at": _dt.datetime.utcnow(),
    })
    return len(channels)

async def refresh_playlist(db: DB, bot: Bot, user_id: int, proxy: str | None = None) -> Optional[int]:
    pl = await db.get_playlist(user_id)
    if not pl:
        return None
    st = pl.get("source_type")
    src = pl.get("source")
    try:
        if st == "url" and src:
            return await save_playlist_from_url(db, user_id, src, proxy=proxy)
        if st == "file" and src:
            return await save_playlist_from_file(db, bot, user_id, src)
    except Exception:
        # ignore refresh errors; keep last known playlist
        return None
    return None

async def resolve_channel(db: DB, user_id: int, channel_name: str) -> Optional[Dict[str, Any]]:
    pl = await db.get_playlist(user_id)
    if not pl:
        return None
    want = _norm_name(channel_name)
    for ch in pl.get("channels", []):
        if _norm_name(ch.get("name", "")) == want:
            return ch
    # try fuzzy contains
    for ch in pl.get("channels", []):
        if want in _norm_name(ch.get("name","")):
            return ch
    return None

async def maybe_refresh_for_active(db: DB, bot: Bot, user_id: int, proxy: str | None = None) -> None:
    """
    Best-effort: refresh user's playlist more often while they are recording.
    """
    pl = await db.get_playlist(user_id)
    if not pl:
        return
    last = pl.get("last_refreshed_at")
    if not last:
        await refresh_playlist(db, bot, user_id, proxy=proxy)
        return
    if isinstance(last, str):
        # old format
        return
    age = (_dt.datetime.utcnow() - last).total_seconds()
    if age >= ACTIVE_PLAYLIST_REFRESH_SEC:
        await refresh_playlist(db, bot, user_id, proxy=proxy)

async def refresh_all_playlists_job(db: DB, bot: Bot, proxy: str | None = None) -> int:
    """
    Refresh URL-based playlists globally.
    Returns number of playlists refreshed successfully (best effort).
    """
    count = 0
    cursor = db.db["playlists"].find({"source_type": "url"})
    async for pl in cursor:
        uid = pl.get("user_id")
        if not isinstance(uid, int):
            continue
        res = await refresh_playlist(db, bot, uid, proxy=proxy)
        if res is not None:
            count += 1
    return count
