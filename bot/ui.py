# bot/ui.py
# ✨ Enhanced Edition (Hot / Cold / Dark)
# - Theme-aware Recording/Downloading UI + Upload UI (separate)
# - LIVE mode supported (no ETA shown)
# - Safe theme storage helpers: get_theme_for_user / set_theme_for_user
# - No breaking changes to other files (same function names)
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional, Any


# =========================
# Theme Core
# =========================

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
        filled = int(round((p / 100.0) * width))
        filled = max(0, min(width, filled))
        return (self.style.fill * filled) + (self.style.empty * (width - filled))

    def fmt_speed(self, bps: float) -> str:
        if bps <= 0:
            return "0 B/s"
        units = ["B/s", "KB/s", "MB/s", "GB/s"]
        v = float(bps)
        u = 0
        while v >= 1024 and u < len(units) - 1:
            v /= 1024.0
            u += 1
        return f"{v:.2f} {units[u]}"

    def fmt_eta(self, seconds: float | None) -> str:
        # For LIVE / unknown: return empty string
        if seconds is None or seconds <= 0 or math.isinf(seconds):
            return ""
        s = int(seconds)
        h = s // 3600
        m = (s % 3600) // 60
        sec = s % 60
        if h > 0:
            return f"{h:02d}:{m:02d}:{sec:02d}"
        return f"{m:02d}:{sec:02d}"

    # -------- helpers --------

    @staticmethod
    def _hms_to_seconds(hms: str) -> Optional[int]:
        """
        Accepts "HH:MM:SS" or "MM:SS".
        Returns seconds or None if not parseable.
        """
        if not hms:
            return None
        parts = hms.strip().split(":")
        if len(parts) == 2:
            try:
                m = int(parts[0])
                s = int(parts[1])
                return m * 60 + s
            except Exception:
                return None
        if len(parts) == 3:
            try:
                h = int(parts[0])
                m = int(parts[1])
                s = int(parts[2])
                return h * 3600 + m * 60 + s
            except Exception:
                return None
        return None

    def _minutes_label(self, elapsed: str | None) -> str:
        """
        Cold/Hot want: "30 Minutes" (nice human label).
        If time is >= 1 hour, keep HH:MM:SS.
        """
        if not elapsed:
            return "0 Minutes"
        sec = self._hms_to_seconds(elapsed)
        if sec is None:
            return elapsed
        if sec >= 3600:
            # keep HH:MM:SS style for long sessions
            h = sec // 3600
            m = (sec % 3600) // 60
            s = sec % 60
            return f"{h:02d}:{m:02d}:{s:02d}"
        minutes = max(0, int(round(sec / 60.0)))
        return f"{minutes} Minutes"

    # =========================
    # Default fallback layouts
    # (Themes override these)
    # =========================
    def progress_recording(
        self,
        *,
        filename: str,
        is_live: bool,
        bar: str,
        percent: float | None,
        speed: str,
        eta: str,
        elapsed: str | None,
        total: str | None,
    ) -> str:
        # safe default
        head = f"📽️ Downloading Stream"
        if is_live:
            return self._t(
                f"{head}\n{self.style.line}\n📄 File: {filename}\n📈 Prog: [{bar}] LIVE\n🚀 Speed: {speed}\n⏱️ Time: {elapsed or '00:00:00'}"
            )
        pct = int(percent or 0)
        time_str = elapsed or "00:00:00"
        return self._t(
            f"{head}\n{self.style.line}\n📄 File: {filename}\n📈 Prog: [{bar}] {pct}%\n🚀 Speed: {speed}\n⏱️ Time: {time_str}"
        )

    def progress_upload(
        self,
        *,
        part_label: str | None,
        bar: str,
        percent: float,
        speed: str,
        eta: str,
    ) -> str:
        head = f"⬆️ Uploading Data (Part {part_label})" if part_label else "⬆️ Uploading Data"
        return self._t(
            f"{head}\n{self.style.line}\n📈 Prog: [{bar}] {int(percent)}%\n🚀 Speed: {speed}\n⏳ ETA: {eta}"
        )


# =========================
# COLD (Professional)
# =========================
class ColdTheme(BaseTheme):
    name = "cold"
    style = ThemeStyle(
        fill="▰",
        empty="▱",
        line="━━━━━━━━━━━━━━━━━━━━━━",
        ok="✅",
        err="❌",
        info="ℹ️",
        warn="⚠️",
        upper=False,
    )

    def progress_recording(self, **kw: Any) -> str:
        filename: str = kw["filename"]
        is_live: bool = kw["is_live"]
        bar: str = kw["bar"]
        percent: float | None = kw["percent"]
        speed: str = kw["speed"]
        elapsed: str | None = kw.get("elapsed")
        total: str | None = kw.get("total")

        head = "📽️ Downloading Stream"
        if is_live:
            # LIVE: no ETA, no percent
            return (
                f"{head}\n{self.style.line}\n"
                f"📄 File: {filename}\n"
                f"📈 Prog: {bar} LIVE\n"
                f"🚀 Speed: {speed}\n"
                f"⏱️ Time: {self._minutes_label(elapsed)}"
            )

        pct = int(percent or 0)
        # fixed: show percent and time label
        time_label = self._minutes_label(elapsed)
        # If total is available you can show it too, but your sample shows only minutes.
        return (
            f"{head}\n{self.style.line}\n"
            f"📄 File: {filename}\n"
            f"📈 Prog: {bar} {pct}%\n"
            f"🚀 Speed: {speed}\n"
            f"⏱️ Time: {time_label}"
        )

    def progress_upload(self, **kw: Any) -> str:
        part: str | None = kw.get("part_label")
        bar: str = kw["bar"]
        percent: float = float(kw["percent"])
        speed: str = kw["speed"]
        eta: str = kw["eta"] or ""

        head = f"⬆️ Uploading Data (Part {part})" if part else "⬆️ Uploading Data"
        return (
            f"{head}\n{self.style.line}\n"
            f"📈 Prog: {bar} {int(percent)}%\n"
            f"🚀 Speed: {speed}\n"
            f"⏳ ETA: {eta}"
        )


# =========================
# HOT (Trolling/Roasting vibe, safe)
# =========================
class HotTheme(BaseTheme):
    name = "hot"
    style = ThemeStyle(
        fill="█",
        empty="░",
        line="━━━━━━━━━━━━━━━━━━━━━━",
        ok="✅",
        err="❌",
        info="😈",
        warn="🧯",
        upper=True,  # the sample shows uppercase-ish headers; we can keep upper True for hot feel
    )

    def progress_recording(self, **kw: Any) -> str:
        filename: str = kw["filename"]
        is_live: bool = kw["is_live"]
        bar: str = kw["bar"]
        percent: float | None = kw["percent"]
        speed: str = kw["speed"]
        elapsed: str | None = kw.get("elapsed")

        head = self._t("🔥 DOWNLOADING TARGET")
        if is_live:
            return self._t(
                f"{head}\n{self.style.line}\n"
                f"🎯 Target: {filename}\n"
                f"🚀 Status: [{bar}] LIVE\n"
                f"⚡ Rate: {speed}\n"
                f"⏱️ Time: {self._minutes_label(elapsed)}"
            )

        pct = int(percent or 0)
        return self._t(
            f"{head}\n{self.style.line}\n"
            f"🎯 Target: {filename}\n"
            f"🚀 Status: [{bar}] {pct}%\n"
            f"⚡ Rate: {speed}\n"
            f"⏱️ Time: {self._minutes_label(elapsed)}"
        )

    def progress_upload(self, **kw: Any) -> str:
        part: str | None = kw.get("part_label")
        bar: str = kw["bar"]
        percent: float = float(kw["percent"])
        speed: str = kw["speed"]
        eta: str = kw["eta"] or ""

        head = self._t("🔥 UPLOADING TARGET")
        part_line = f"📦 Box: Part {part}\n" if part else ""
        return self._t(
            f"{head}\n{self.style.line}\n"
            f"{part_line}"
            f"🚀 Status: [{bar}] {int(percent)}%\n"
            f"⚡ Rate: {speed}\n"
            f"⏳ Left: {eta}"
        )


# =========================
# DARK (Operator vibe)
# =========================
class DarkTheme(BaseTheme):
    name = "dark"
    style = ThemeStyle(
        fill="▓",
        empty="░",
        line="======================",
        ok="✅",
        err="❌",
        info="🕶️",
        warn="⚠️",
        upper=True,
    )

    def progress_recording(self, **kw: Any) -> str:
        filename: str = kw["filename"]
        is_live: bool = kw["is_live"]
        bar: str = kw["bar"]
        percent: float | None = kw["percent"]
        speed: str = kw["speed"]
        elapsed: str | None = kw.get("elapsed")  # dark sample wants exact time
        total: str | None = kw.get("total")

        head = "[+] DOWNLOADING DATA"
        if is_live:
            # LIVE: show time up counter, no ETA
            return self._t(
                f"{head}\n{self.style.line}\n"
                f"FILE : {filename}\n"
                f"PROG : [{bar}] LIVE\n"
                f"RATE : {speed}\n"
                f"TIME : {elapsed or '00:00:00'}"
            )

        pct = int(percent or 0)
        # Dark sample shows TIME: HH:MM:SS (elapsed)
        time_line = elapsed or "00:00:00"
        # If total exists you can append it, but sample uses only elapsed
        _ = total  # keep parameter accepted
        return self._t(
            f"{head}\n{self.style.line}\n"
            f"FILE : {filename}\n"
            f"PROG : [{bar}] {pct}%\n"
            f"RATE : {speed}\n"
            f"TIME : {time_line}"
        )

    def progress_upload(self, **kw: Any) -> str:
        part: str | None = kw.get("part_label")
        bar: str = kw["bar"]
        percent: float = float(kw["percent"])
        speed: str = kw["speed"]
        eta: str = kw["eta"] or ""

        head = "[+] UPLOADING DATA"
        part_line = f"PART : {part}\n" if part else ""
        return self._t(
            f"{head}\n{self.style.line}\n"
            f"{part_line}"
            f"PROG : [{bar}] {int(percent)}%\n"
            f"RATE : {speed}\n"
            f"ETA  : {eta}"
        )


# =========================
# Theme registry
# =========================
THEMES: Dict[str, BaseTheme] = {
    "cold": ColdTheme(),
    "hot": HotTheme(),
    "dark": DarkTheme(),
}


def get_theme(name: str) -> BaseTheme:
    return THEMES.get((name or "cold").lower(), THEMES["cold"])


# =========================
# Theme storage helpers
# (used by /hot /cold /dark and access.py)
# =========================
try:
    from .config import DEFAULT_THEME
except Exception:
    DEFAULT_THEME = "cold"

_THEME_NAMES = {"hot", "cold", "dark"}


def _normalize_theme(t: str) -> str:
    t = (t or DEFAULT_THEME).lower()
    return t if t in _THEME_NAMES else DEFAULT_THEME


async def get_theme_for_user(context, user_id: int) -> str:
    """
    Works with PTB CallbackContext (context.application) OR Application object.
    """
    app = getattr(context, "application", context)
    bot_data = getattr(app, "bot_data", {}) or {}

    db = bot_data.get("db")
    if db:
        try:
            doc = await db.get_user(user_id)
            return _normalize_theme(doc.get("theme"))
        except Exception:
            pass

    mem = bot_data.setdefault("themes", {})
    return _normalize_theme(mem.get(user_id, DEFAULT_THEME))


async def set_theme_for_user(context, user_id: int, theme: str) -> str:
    """
    Stores in Mongo if available, else in-memory.
    Returns normalized theme.
    """
    theme = _normalize_theme(theme)

    app = getattr(context, "application", context)
    bot_data = getattr(app, "bot_data", {}) or {}

    db = bot_data.get("db")
    if db:
        try:
            await db.update_user(user_id, {"theme": theme})
            return theme
        except Exception:
            pass

    mem = bot_data.setdefault("themes", {})
    mem[user_id] = theme
    return theme
