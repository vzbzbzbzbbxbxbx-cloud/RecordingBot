# bot/ui.py
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import DEFAULT_THEME
from .db import DB

# -------------------------
# Theme base
# -------------------------

@dataclass
class Theme:
    name: str

    def bar(self, percent: float, width: int = 10) -> str:
        p = max(0.0, min(100.0, float(percent)))
        filled = int(round((p / 100.0) * width))
        filled = max(0, min(width, filled))
        return "▰" * filled + "▱" * (width - filled)

    def fmt_speed(self, bytes_per_sec: float) -> str:
        if bytes_per_sec <= 0:
            return "0 B/s"
        units = ["B/s", "KB/s", "MB/s", "GB/s"]
        v = float(bytes_per_sec)
        u = 0
        while v >= 1024 and u < len(units) - 1:
            v /= 1024.0
            u += 1
        return f"{v:.2f} {units[u]}"

    def fmt_eta(self, seconds: float) -> str:
        if seconds is None or seconds <= 0 or math.isinf(seconds):
            return "--:--:--"
        s = int(seconds)
        h = s // 3600
        m = (s % 3600) // 60
        sec = s % 60
        return f"{h:02d}:{m:02d}:{sec:02d}"

_THEMES = {
    "cold": Theme("cold"),
    "hot": Theme("hot"),
    "dark": Theme("dark"),
}

def get_theme(theme_name: str) -> Theme:
    return _THEMES.get((theme_name or DEFAULT_THEME).lower(), _THEMES["cold"])

async def get_theme_for_user(context, user_id: int) -> str:
    """
    Fetch user's theme from MongoDB if available, otherwise from memory.
    Stored in bot_data["db"].
    """
    db: DB | None = context.application.bot_data.get("db") if context and getattr(context, "application", None) else None
    if db:
        doc = await db.get_user(user_id)
        t = (doc.get("theme") or DEFAULT_THEME).lower()
        return t if t in _THEMES else DEFAULT_THEME
    # fallback memory
    mem = context.application.bot_data.setdefault("themes", {})
    return mem.get(user_id, DEFAULT_THEME)

async def set_theme_for_user(context, user_id: int, theme: str) -> None:
    theme = (theme or DEFAULT_THEME).lower()
    if theme not in _THEMES:
        theme = DEFAULT_THEME
    db: DB | None = context.application.bot_data.get("db") if context and getattr(context, "application", None) else None
    if db:
        await db.update_user(user_id, {"theme": theme})
        return
    mem = context.application.bot_data.setdefault("themes", {})
    mem[user_id] = theme
