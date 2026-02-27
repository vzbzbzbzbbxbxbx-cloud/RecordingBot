# bot/utils/uploader.py
from __future__ import annotations

import asyncio
import mimetypes
import os
from pathlib import Path
from typing import Optional, Dict, Any

import aiohttp
from aiohttp import payload

from ..config import BOT_TOKEN
from ..progress import ProgressTracker

API_BASE = "https://api.telegram.org"

class _ProgressPayload(payload.Payload):
    def __init__(self, value, *, tracker: ProgressTracker, filename: str, content_type: str):
        super().__init__(value, content_type=content_type, filename=filename)
        self._value = value
        self._tracker = tracker

    async def write(self, writer) -> None:
        sent = 0
        chunk_size = 256 * 1024
        while True:
            chunk = self._value.read(chunk_size)
            if not chunk:
                break
            await writer.write(chunk)
            sent += len(chunk)
            await self._tracker.update(sent, done=False)
        await self._tracker.update(sent, done=True)

async def send_video_with_progress(
    chat_id: int,
    video_path: Path,
    thumb_path: Optional[Path],
    caption: str,
    tracker: ProgressTracker,
    reply_to_message_id: Optional[int] = None,
    supports_streaming: bool = True,
) -> Dict[str, Any]:
    """
    Uploads video via Bot API with a real progress tracker.
    """
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    url = f"{API_BASE}/bot{BOT_TOKEN}/sendVideo"

    # content type
    ctype = mimetypes.guess_type(str(video_path))[0] or "application/octet-stream"

    data = aiohttp.FormData()
    data.add_field("chat_id", str(chat_id))
    data.add_field("caption", caption)
    data.add_field("supports_streaming", "true" if supports_streaming else "false")
    if reply_to_message_id:
        data.add_field("reply_to_message_id", str(reply_to_message_id))

    # video
    f = open(video_path, "rb")
    data.add_field(
        "video",
        _ProgressPayload(f, tracker=tracker, filename=video_path.name, content_type=ctype),
    )

    # thumbnail (optional)
    thumb_file = None
    if thumb_path and thumb_path.exists():
        thumb_file = open(thumb_path, "rb")
        data.add_field("thumbnail", thumb_file, filename=thumb_path.name, content_type="image/jpeg")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None)) as session:
        async with session.post(url, data=data) as resp:
            js = await resp.json(content_type=None)
            if thumb_file:
                thumb_file.close()
            f.close()
            if not js.get("ok"):
                raise RuntimeError(f"Telegram sendVideo failed: {js}")
            return js
