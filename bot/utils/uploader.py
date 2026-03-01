# bot/utils/uploader.py
from __future__ import annotations

import asyncio
import mimetypes
import os
import time
from pathlib import Path
from typing import Optional, Dict, Any

import aiohttp
from aiohttp import payload

from ..config import BOT_TOKEN
from ..progress import ProgressTracker

API_BASE = "https://api.telegram.org"

# Use MTProto (Pyrogram) for big uploads
BOT_API_SAFE_MAX_BYTES = 45 * 1024 * 1024  # 45MB (safe trigger before 50MB problems)

# Env (do NOT require changing config.py)
TG_API_ID = int(os.getenv("TG_API_ID", "0") or "0")
TG_API_HASH = (os.getenv("TG_API_HASH", "") or "").strip()

_MT_LOCK = asyncio.Lock()
_MT_CLIENT = None
_MT_STARTED = False


# -----------------------------
# Bot API streaming payload
# -----------------------------
class _ProgressPayload(payload.Payload):
    """
    Streams file chunks and updates ProgressTracker with ABSOLUTE bytes sent.
    Sets size so aiohttp can build correct multipart Content-Length.
    """

    def __init__(self, fileobj, *, tracker: ProgressTracker, filename: str, content_type: str):
        super().__init__(fileobj, content_type=content_type, filename=filename)
        self._file = fileobj
        self._tracker = tracker
        try:
            self._size = os.fstat(fileobj.fileno()).st_size
        except Exception:
            self._size = None

    async def write(self, writer) -> None:
        sent = 0
        chunk_size = 256 * 1024

        # initial ping
        try:
            await self._tracker.update(0, done=False)
        except Exception:
            pass

        while True:
            chunk = self._file.read(chunk_size)
            if not chunk:
                break
            await writer.write(chunk)
            sent += len(chunk)
            try:
                await self._tracker.update(sent, done=False)
            except Exception:
                pass

        try:
            await self._tracker.update(sent, done=True)
        except Exception:
            pass


async def _send_video_bot_api(
    *,
    chat_id: int,
    video_path: Path,
    thumb_path: Optional[Path],
    caption: str,
    tracker: ProgressTracker,
    reply_to_message_id: Optional[int],
    supports_streaming: bool,
) -> Dict[str, Any]:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    url = f"{API_BASE}/bot{BOT_TOKEN}/sendVideo"
    ctype = mimetypes.guess_type(str(video_path))[0] or "application/octet-stream"

    video_f = open(video_path, "rb")
    thumb_f = None

    try:
        data = aiohttp.FormData()
        data.add_field("chat_id", str(chat_id))
        data.add_field("caption", caption)
        data.add_field("supports_streaming", "true" if supports_streaming else "false")
        if reply_to_message_id:
            data.add_field("reply_to_message_id", str(reply_to_message_id))

        # video field with filename + content_type
        prog = _ProgressPayload(video_f, tracker=tracker, filename=video_path.name, content_type=ctype)
        data.add_field("video", prog, filename=video_path.name, content_type=ctype)

        if thumb_path and thumb_path.exists():
            thumb_f = open(thumb_path, "rb")
            data.add_field("thumbnail", thumb_f, filename=thumb_path.name, content_type="image/jpeg")

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None)) as session:
            async with session.post(url, data=data) as resp:
                try:
                    js = await resp.json(content_type=None)
                except Exception:
                    txt = await resp.text()
                    raise RuntimeError(f"BotAPI sendVideo HTTP {resp.status}: {txt[:500]}")
                if not js.get("ok"):
                    raise RuntimeError(f"BotAPI sendVideo failed: {js}")
                return js

    finally:
        try:
            if thumb_f:
                thumb_f.close()
        except Exception:
            pass
        try:
            video_f.close()
        except Exception:
            pass


# -----------------------------
# MTProto (Pyrogram) uploader
# -----------------------------
async def _get_mt_client():
    """
    Lazy-init a single Pyrogram client (bot token session).
    """
    global _MT_CLIENT, _MT_STARTED

    if not TG_API_ID or not TG_API_HASH:
        raise RuntimeError("TG_API_ID/TG_API_HASH not set (required for MTProto upload)")

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    async with _MT_LOCK:
        if _MT_CLIENT is None:
            from pyrogram import Client  # local import
            # Session file: creates "rippingbot_mtproto.session" in cwd
            _MT_CLIENT = Client(
                "rippingbot_mtproto",
                api_id=TG_API_ID,
                api_hash=TG_API_HASH,
                bot_token=BOT_TOKEN,
                in_memory=False,
            )

        if not _MT_STARTED:
            await _MT_CLIENT.start()
            _MT_STARTED = True

        return _MT_CLIENT


async def _send_video_mtproto(
    *,
    chat_id: int,
    video_path: Path,
    thumb_path: Optional[Path],
    caption: str,
    tracker: ProgressTracker,
    reply_to_message_id: Optional[int],
    supports_streaming: bool,
) -> Dict[str, Any]:
    """
    Upload via MTProto (Pyrogram) with progress callback -> tracker.update()
    """
    app = await _get_mt_client()

    total = int(video_path.stat().st_size) if video_path.exists() else 1
    last_push = 0.0

    loop = asyncio.get_running_loop()

    def progress(current: int, total_bytes: int, *_args):
        nonlocal last_push
        now = time.time()
        # throttle updates to avoid overload
        if now - last_push < 0.35 and current < total_bytes:
            return
        last_push = now

        # tracker.update is async -> schedule
        try:
            loop.create_task(tracker.update(int(current), done=False))
        except Exception:
            pass

    # initial ping
    try:
        await tracker.update(0, done=False)
    except Exception:
        pass

    # send_video first (playable media). If Telegram rejects, fallback to send_document.
    try:
        m = await app.send_video(
            chat_id=chat_id,
            video=str(video_path),
            thumb=str(thumb_path) if thumb_path and thumb_path.exists() else None,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
            supports_streaming=supports_streaming,
            progress=progress,
        )
        try:
            await tracker.update(total, done=True)
        except Exception:
            pass
        return {"ok": True, "result": {"message_id": getattr(m, "id", None)}}

    except Exception:
        # fallback: still uploads, but as document (may not be “streaming”)
        m = await app.send_document(
            chat_id=chat_id,
            document=str(video_path),
            caption=caption,
            reply_to_message_id=reply_to_message_id,
            progress=progress,
        )
        try:
            await tracker.update(total, done=True)
        except Exception:
            pass
        return {"ok": True, "result": {"message_id": getattr(m, "id", None)}}


# -----------------------------
# Public API (do NOT change signature)
# -----------------------------
async def send_video_with_progress(
    *,
    chat_id: int,
    video_path: Path,
    thumb_path: Optional[Path],
    caption: str,
    tracker: ProgressTracker,
    reply_to_message_id: Optional[int] = None,
    supports_streaming: bool = True,
) -> Dict[str, Any]:
    """
    Hybrid uploader:
    - Small files -> Bot API (fast, no extra deps)
    - Big files or Bot API fail -> MTProto (Pyrogram) for reliable large uploads
    """
    size = int(video_path.stat().st_size) if video_path.exists() else 0

    # If MTProto creds exist and file is big -> use MTProto directly
    if TG_API_ID and TG_API_HASH and size >= BOT_API_SAFE_MAX_BYTES:
        return await _send_video_mtproto(
            chat_id=chat_id,
            video_path=video_path,
            thumb_path=thumb_path,
            caption=caption,
            tracker=tracker,
            reply_to_message_id=reply_to_message_id,
            supports_streaming=supports_streaming,
        )

    # Otherwise try Bot API first
    try:
        return await _send_video_bot_api(
            chat_id=chat_id,
            video_path=video_path,
            thumb_path=thumb_path,
            caption=caption,
            tracker=tracker,
            reply_to_message_id=reply_to_message_id,
            supports_streaming=supports_streaming,
        )
    except Exception as e:
        # fallback to MTProto if possible
        if TG_API_ID and TG_API_HASH:
            return await _send_video_mtproto(
                chat_id=chat_id,
                video_path=video_path,
                thumb_path=thumb_path,
                caption=caption,
                tracker=tracker,
                reply_to_message_id=reply_to_message_id,
                supports_streaming=supports_streaming,
            )
        raise e
