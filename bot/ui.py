# bot/ui.py (theme-specific recording/upload UI separated)
from __future__ import annotations
import math
from dataclasses import dataclass

@dataclass(frozen=True)
class ThemeStyle:
    fill: str
    empty: str
    line: str
    ok: str
    err: str
    info: str
    warn: str
    upper: bool = False

class BaseTheme:
    name = "base"
    style: ThemeStyle

    def _t(self, s: str) -> str:
        return s.upper() if self.style.upper else s

    def bar(self, percent: float, width: int = 10) -> str:
        p = max(0.0, min(100.0, float(percent)))
        filled = int(round((p/100.0)*width))
        filled = max(0, min(width, filled))
        return (self.style.fill * filled) + (self.style.empty * (width-filled))

    def fmt_speed(self, bps: float) -> str:
        if bps <= 0:
            return "0 B/s"
        units = ["B/s", "KB/s", "MB/s", "GB/s"]
        v = float(bps); u = 0
        while v >= 1024 and u < len(units)-1:
            v /= 1024.0; u += 1
        return f"{v:.2f} {units[u]}"

    def fmt_eta(self, seconds: float | None) -> str:
        # IMPORTANT: LIVE/unknown হলে ETA দেখাবে না
        if seconds is None or seconds <= 0 or math.isinf(seconds):
            return ""
        s = int(seconds)
        h = s // 3600
        m = (s % 3600) // 60
        sec = s % 60
        return f"{h:02d}:{m:02d}:{sec:02d}"

    # -------------------------
    # Default layouts (themes can override)
    # -------------------------
    def progress_recording(self, *, filename: str, is_live: bool, bar: str,
                         percent: float | None, speed: str,
                         eta: str, elapsed: str | None, total: str | None) -> str:
        # default: simple 2 lines
        head = f"📽️ Recording: {filename}"
        if is_live:
            l1 = f"🔴 LIVE  {bar}"
            l2 = f"⚡ {speed}" + (f" • 🕒 {elapsed}" if elapsed else "")
        else:
            pct = int(percent or 0)
            l1 = f"{bar} {pct}%"
            l2 = f"⚡ {speed}" + (f" • ⏳ ETA {eta}" if eta else "")
            if elapsed and total:
                l2 += f" • 🕒 {elapsed}/{total}"
        return self._t(f"{head}\n{self.style.line}\n{l1}\n{l2}")

    def progress_upload(self, *, part_label: str | None, bar: str, percent: float,
                      speed: str, eta: str) -> str:
        head = "⬆️ Uploading"
        if part_label:
            head += f" • Part {part_label}"
        l1 = f"{bar} {int(percent)}%"
        l2 = f"⚡ {speed}" + (f" • ⏳ ETA {eta}" if eta else "")
        return self._t(f"{head}\n{self.style.line}\n{l1}\n{l2}")


# -------------------------
# COLD (professional)
# -------------------------
class ColdTheme(BaseTheme):
    name = "cold"
    style = ThemeStyle(fill="▰", empty="▱", line="━━━━━━━━━━━━━━━━━━━━━━",
                       ok="✅", err="❌", info="ℹ️", warn="⚠️", upper=False)

    def progress_recording(self, **kw) -> str:
        # more professional formatting
        filename = kw["filename"]
        is_live = kw["is_live"]
        bar = kw["bar"]
        percent = kw["percent"]
        speed = kw["speed"]
        eta = kw["eta"]
        elapsed = kw["elapsed"]
        total = kw["total"]

        head = f"📽️ Recording"
        l0 = f"File: {filename}"
        if is_live:
            l1 = f"Mode: LIVE 🔴"
            l2 = f"{bar}"
            l3 = f"Speed: {speed}" + (f" • Elapsed: {elapsed}" if elapsed else "")
        else:
            l1 = f"Mode: Fixed"
            l2 = f"{bar} {int(percent or 0)}%"
            l3 = f"Speed: {speed}" + (f" • ETA: {eta}" if eta else "")
            if elapsed and total:
                l3 += f" • Time: {elapsed}/{total}"

        return f"{head}\n{self.style.line}\n{l0}\n{l1}\n{l2}\n{l3}"


# -------------------------
# HOT (roast/troll vibe but safe)
# -------------------------
class HotTheme(BaseTheme):
    name = "hot"
    style = ThemeStyle(fill="█", empty="░", line="━━━━━━━━━━🔥━━━━━━━━━━",
                       ok="✅", err="❌", info="😈", warn="🧯", upper=False)

    def progress_recording(self, **kw) -> str:
        filename = kw["filename"]
        is_live = kw["is_live"]
        bar = kw["bar"]
        percent = kw["percent"]
        speed = kw["speed"]
        eta = kw["eta"]
        elapsed = kw["elapsed"]
        total = kw["total"]

        head = f"😈 Recording Cooking…"
        if is_live:
            l1 = f"🔴 LIVE • {filename}"
            l2 = f"{bar}  (No ETA for LIVE)"
            l3 = f"⚡ {speed}" + (f" • 🕒 {elapsed}" if elapsed else "")
        else:
            l1 = f"📽️ {filename}"
            l2 = f"{bar} {int(percent or 0)}%"
            l3 = f"⚡ {speed}" + (f" • ⏳ {eta}" if eta else "")
            if elapsed and total:
                l3 += f" • 🕒 {elapsed}/{total}"

        return f"{head}\n{self.style.line}\n{l1}\n{l2}\n{l3}"

    def progress_upload(self, **kw) -> str:
        part = kw["part_label"]
        bar = kw["bar"]
        percent = kw["percent"]
        speed = kw["speed"]
        eta = kw["eta"]
        head = f"🚀 Uploading Like a Rocket"
        l1 = f"📦 Part: {part or 'single'}"
        l2 = f"{bar} {int(percent)}%"
        l3 = f"⚡ {speed}" + (f" • ⏳ {eta}" if eta else "")
        return f"{head}\n{self.style.line}\n{l1}\n{l2}\n{l3}"


# -------------------------
# DARK (operator vibe)
# -------------------------
class DarkTheme(BaseTheme):
    name = "dark"
    style = ThemeStyle(fill="▓", empty="░", line="━━━━━━━━━━█ SYSTEM █━━━━━━━━━━",
                       ok="✅", err="❌", info="🕶️", warn="⚠️", upper=True)

    def progress_recording(self, **kw) -> str:
        filename = kw["filename"]
        is_live = kw["is_live"]
        bar = kw["bar"]
        percent = kw["percent"]
        speed = kw["speed"]
        eta = kw["eta"]
        elapsed = kw["elapsed"]
        total = kw["total"]

        head = "SYSTEM CAPTURE ONLINE"
        if is_live:
            l1 = f"MODE=LIVE  TARGET={filename}"
            l2 = f"{bar}"
            l3 = f"RATE={speed}" + (f"  ELAPSED={elapsed}" if elapsed else "")
        else:
            l1 = f"MODE=FIXED  TARGET={filename}"
            l2 = f"{bar}  PCT={int(percent or 0)}"
            l3 = f"RATE={speed}" + (f"  ETA={eta}" if eta else "")
            if elapsed and total:
                l3 += f"  TIME={elapsed}/{total}"

        return self._t(f"{head}\n{self.style.line}\n{l1}\n{l2}\n{l3}")


THEMES = {"cold": ColdTheme(), "hot": HotTheme(), "dark": DarkTheme()}

def get_theme(name: str):
    return THEMES.get((name or "cold").lower(), THEMES["cold"])
# =========================
# Theme storage helpers
# Used by: bot/access.py and /hot /cold /dark handlers
# =========================

try:
    from .config import DEFAULT_THEME
except Exception:
    DEFAULT_THEME = "cold"

_THEMES = {"hot", "cold", "dark"}


def _normalize_theme(t: str) -> str:
    t = (t or DEFAULT_THEME).lower()
    return t if t in _THEMES else DEFAULT_THEME


async def get_theme_for_user(context, user_id: int) -> str:
    """
    Returns user's theme (hot/cold/dark).
    Tries MongoDB first (context.application.bot_data["db"]).
    Falls back to in-memory store.
    """
    # Try DB (Mongo)
    db = None
    try:
        db = context.application.bot_data.get("db")
    except Exception:
        db = None

    if db:
        try:
            doc = await db.get_user(user_id)
            return _normalize_theme(doc.get("theme"))
        except Exception:
            pass

    # Fallback memory
    try:
        mem = context.application.bot_data.setdefault("themes", {})
        return _normalize_theme(mem.get(user_id, DEFAULT_THEME))
    except Exception:
        return DEFAULT_THEME


async def set_theme_for_user(context, user_id: int, theme: str) -> str:
    """
    Sets user's theme in MongoDB if available, else in-memory.
    Returns the normalized theme actually stored.
    """
    theme = _normalize_theme(theme)

    db = None
    try:
        db = context.application.bot_data.get("db")
    except Exception:
        db = None

    if db:
        try:
            await db.update_user(user_id, {"theme": theme})
            return theme
        except Exception:
            pass

    # Fallback memory
    try:
        mem = context.application.bot_data.setdefault("themes", {})
        mem[user_id] = theme
    except Exception:
        pass

    return theme
