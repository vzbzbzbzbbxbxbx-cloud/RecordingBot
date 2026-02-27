# bot/utils/http.py
from __future__ import annotations

from typing import Dict, Tuple, Optional
import aiohttp

async def fetch_text(url: str, headers: Optional[Dict[str, str]] = None, proxy: Optional[str] = None, timeout: int = 20) -> Tuple[str, Dict[str, str]]:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.get(url, headers=headers, proxy=proxy) as resp:
            resp.raise_for_status()
            text = await resp.text(errors="ignore")
            meta = {
                "etag": resp.headers.get("ETag", ""),
                "last_modified": resp.headers.get("Last-Modified", ""),
                "content_type": resp.headers.get("Content-Type", ""),
            }
            return text, meta
