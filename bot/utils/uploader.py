# bot/utils/uploader.py  ✨ Enhanced Edition
# - Real streaming upload to Telegram Bot API (sendVideo)
# - Proper progress tracking (no 0% stuck)
# - Sets file size so aiohttp can build correct multipart Content-Length
# - Safe cleanup + better error reporting
from __future__ import annotations

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
    """
    Streams file chunks and updates ProgressTracker with ABSOLUTE bytes sent.
    IMPORTANT: sets self._size so aiohttp can compute Content-Length properly.
    """

    def __init__(self, fileobj, *, tracker: ProgressTracker, filename: str, content_type: str):
        super().__init__(fileobj, content_type=content_type, filename=filename)
        self._file = fileobj
        self._tracker = tracker

        # ✅ Critical: Provide size to aiohttp (prevents "0% stuck" on some hosts)
        try:
            self._size = os.fstat(fileobj.fileno()).st_size
        except Exception:
            self._size = None

    async def write(self, writer) -> None:
        sent = 0
        chunk_size = 256 * 1024  # 256KB

        # ✅ Initial ping so UI can move from dead 0-state immediately
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
    Uploads video via Telegram Bot API with a REAL progress tracker.
    Keeps the same function name/signature your other files already use.
    """
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    url = f"{API_BASE}/bot{BOT_TOKEN}/sendVideo"

    # content type
    ctype = mimetypes.guess_type(str(video_path))[0] or "application/octet-stream"

    video_f = open(video_path, "rb")
    thumb_f = None

    try:
        data = aiohttp.FormData()
        data.add_field("chat_id", str(chat_id))
        if caption is not None:
            data.add_field("caption", caption)
        data.add_field("supports_streaming", "true" if supports_streaming else "false")

        if reply_to_message_id:
            data.add_field("reply_to_message_id", str(reply_to_message_id))

        # ✅ Video field with explicit filename + content_type (Telegram parses faster/cleaner)
        prog = _ProgressPayload(video_f, tracker=tracker, filename=video_path.name, content_type=ctype)
        data.add_field(
            "video",
            prog,
            filename=video_path.name,
            content_type=ctype,
        )

        # Thumbnail optional
        if thumb_path and thumb_path.exists():
            thumb_f = open(thumb_path, "rb")
            data.add_field(
                "thumbnail",
                thumb_f,
                filename=thumb_path.name,
                content_type="image/jpeg",
            )

        timeout = aiohttp.ClientTimeout(total=None)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, data=data) as resp:
                # Telegram sometimes returns non-json on errors; handle both.
                try:
                    js = await resp.json(content_type=None)
                except Exception:
                    txt = await resp.text()
                    raise RuntimeError(f"Telegram sendVideo HTTP {resp.status}: {txt[:500]}")

                if not js.get("ok"):
                    raise RuntimeError(f"Telegram sendVideo failed: {js}")

                return js

    finally:
        # Ensure final tracker completion if something dies early
        try:
            await tracker.update(tracker.total or 0, done=True)  # best-effort
        except Exception:
            pass

        try:
            if thumb_f:
                thumb_f.close()
        except Exception:
            pass
        try:
            video_f.close()
        except Exception:
            pass
