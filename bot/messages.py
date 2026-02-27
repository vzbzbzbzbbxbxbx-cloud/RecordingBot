# bot/messages.py
"""
messages.py
Central reply catalogue (theme-aware).

Goals:
- Keep ALL text replies here (commands, errors, statuses, progress)
- Expandable to 800+ replies easily
- Themes: hot / cold / dark
- Supports single string OR list of variants per key
- Safe formatting: never crashes on .format
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Any, List, Union, Optional
import hashlib

_THEMES = ("hot", "cold", "dark")
Template = Union[str, List[str]]


def _fallback_theme(theme: str) -> str:
    t = (theme or "cold").lower()
    return t if t in _THEMES else "cold"


def _pick_variant(tpl: Template, seed: Optional[Union[int, str]] = None) -> str:
    """
    If tpl is list -> pick deterministically using seed (stable, not random spam).
    """
    if isinstance(tpl, str):
        return tpl
    if not tpl:
        return ""

    # seed fallback: stable based on list length
    if seed is None:
        seed = 0

    s = str(seed).encode("utf-8")
    h = hashlib.md5(s).hexdigest()  # stable
    idx = int(h[:8], 16) % len(tpl)
    return tpl[idx]


@dataclass(frozen=True)
class Msg:
    """
    Message catalogue accessor.

    Keys are "namespaced" strings, e.g.:
        system.start
        access.group_only
        playlist.added_url
        record.progress
        upload.progress
        limits.daily_exceeded
    """
    MESSAGES: Dict[str, Dict[str, Template]] = None  # type: ignore

    @staticmethod
    def get(theme: str, key: str, seed: Optional[Union[int, str]] = None, **kwargs: Any) -> str:
        """
        theme -> hot/cold/dark
        key   -> message key
        seed  -> used only when the template has multiple variants (list)
        """
        theme = _fallback_theme(theme)

        # 1) Theme catalog
        cat = Msg.MESSAGES.get(theme, {})
        tpl = cat.get(key)

        # 2) Fallback to cold
        if tpl is None:
            tpl = Msg.MESSAGES.get("cold", {}).get(key)

        if tpl is None:
            return f"[missing:{key}]"

        template = _pick_variant(tpl, seed=seed or kwargs.get("task_id") or kwargs.get("user_id"))
        try:
            return template.format(**kwargs)
        except Exception:
            return template  # never crash due to formatting mismatch

    @staticmethod
    def exists(theme: str, key: str) -> bool:
        theme = _fallback_theme(theme)
        return key in Msg.MESSAGES.get(theme, {}) or key in Msg.MESSAGES.get("cold", {})


# -------------------------
# Catalogue
# NOTE: hot/dark only override what they want.
# Everything else auto-falls back to cold.
# -------------------------

COLD: Dict[str, Template] = {
    # Access
    "access.group_only": [
        "❌ This bot can be used only in the allowed group.\n\n✅ Allowed group: `{group_id}`",
        "❌ Restricted here.\n✅ Use this in the authorized group: `{group_id}`",
    ],
    "access.owner_dm_only": [
        "❌ Owner commands are available only in DM for security.",
        "❌ Owner panel is DM-only. Please use private chat.",
    ],

    # System
    "system.start": [
        "✅ Bot online.\n\n• Version: `{version}`\n• Theme: `{theme}`\n\nUse /help to see commands.",
        "✅ Online.\nVersion: `{version}` | Theme: `{theme}`\nUse /help for commands.",
    ],
    "system.help": (
        "📌 Commands\n"
        "━━━━━━━━━━━━━━\n"
        "• /playlist (reply to file or URL)\n"
        "• /channel\n"
        "• /record <link|\"channel\"> <HH:MM:SS> <filename>\n"
        "• /schedule <link|\"channel\"> <time> <filename> [duration]\n"
        "• /cancel\n"
        "• /tasks\n"
        "• /status\n"
        "• /stats\n"
        "• /proxy (owner only)\n"
        "• /auth (reply) 1d / 30d (owner only)\n"
        "• /rm (reply) (owner only)\n"
        "• /trial (reply) 1 / 2 / 3 (owner only)\n"
        "• /hot /cold /dark\n"
    ),
    "system.theme_set": [
        "✅ Theme changed to **{theme}**.",
        "✅ UI set to **{theme}**.",
    ],

    # Playlist
    "playlist.added_url": [
        "✅ Playlist saved from URL.\n• Channels: {count}\n• Auto refresh: every {refresh}s",
        "✅ URL playlist stored.\nChannels indexed: {count}\nRefresh: {refresh}s",
    ],
    "playlist.added_file": [
        "✅ Playlist saved from file.\n• Channels: {count}",
        "✅ File playlist stored.\nChannels: {count}",
    ],
    "playlist.invalid": [
        "❌ Could not parse playlist. Make sure it's a valid M3U / M3U8.",
        "❌ Invalid playlist format. Send a valid M3U/M3U8.",
    ],
    "playlist.none": [
        "❌ No playlist found. Use /playlist and reply to a playlist file or send a URL.",
        "❌ Playlist not set. Use /playlist first.",
    ],
    "playlist.refresh_ok": [
        "✅ Playlist refreshed.\n• Channels: {count}",
        "✅ Playlist updated.\nChannels: {count}",
    ],

    # Channels
    "channel.header": "📺 Available channels ({count})",
    "channel.item": "• {idx}. `{name}`",
    "channel.none": [
        "❌ No channels found. Add a playlist using /playlist.",
        "❌ Channel list empty. Add playlist via /playlist.",
    ],

    # Record flow
    "record.queued": (
        "✅ Added to queue.\n"
        "• Task: `{task_id}`\n"
        "• Source: {source}\n"
        "• Duration: {duration}\n"
        "• Name: `{filename}`"
    ),
    "record.started": (
        "📽️ Recording started.\n"
        "• Task: `{task_id}`\n"
        "• Source: {source}\n"
        "• Duration: {duration}\n"
        "• Output: `{filename}`"
    ),
    "record.cancelled": "❌ Cancelled.\n• Task: `{task_id}`",
    "record.finished": "✅ Done.\n• Task: `{task_id}`\n• Uploaded parts: {parts}",
    "record.failed": "❌ Recording failed.\n• Task: `{task_id}`\n• Reason: {reason}",

    # Progress (fallback only; your ui.py can override with theme.progress_recording/progress_upload)
    "record.progress": (
        "📽️ Recording…\n"
        "• Task: `{task_id}`\n"
        "• File: `{filename}`\n"
        "• Elapsed: `{elapsed}` / `{total}`\n"
        "{bar}\n"
        "⚡ Speed: `{speed}`\n"
        "⏳ ETA: `{eta}`"
    ),
    "upload.progress": (
        "📤 Uploading…\n"
        "• Task: `{task_id}`\n"
        "• Part: `{part}`\n"
        "{bar}\n"
        "✅ {percent}%  |  ⚡ `{speed}`  |  ⏳ `{eta}`"
    ),
    "upload.done": "✅ Uploaded: `{name}`",

    # Tasks
    "tasks.header": "📌 Tasks\n━━━━━━━━━━━━━━",
    "tasks.active": "✅ Active ({count})",
    "tasks.queued": "⏳ Queue ({count})",
    "tasks.item": "• `{task_id}` — {user} — {state} — `{name}`",

    # Limits / Subscription
    "limits.need_trial_or_premium": "❌ You are not premium and you have no trial credits. Ask the owner for /trial or /auth.",
    "limits.daily_exceeded": "❌ Daily limit reached.\nUsed: `{used}` / `{limit}`\nReset: `{reset}`",
    "limits.trial_no_credits": "❌ No trial credits remaining. Ask the owner for /trial.",
    "limits.ok": "✅ Allowed. Remaining today: `{remaining}`",

    # Auth commands
    "auth.only_owner": "❌ Owner only command.",
    "auth.ok": "✅ Premium granted.\nUser: `{user_id}`\nUntil: `{until}`",
    "auth.rm_ok": "✅ Premium removed.\nUser: `{user_id}`",
    "trial.set_ok": "✅ Trial credits set.\nUser: `{user_id}`\nCredits: `{credits}`",

    # Status & Stats
    "status.text": (
        "👤 User: `{user_id}`\n"
        "⭐ Tier: `{tier}`\n"
        "🕒 Used today: `{used}`\n"
        "⏳ Limit today: `{limit}`\n"
        "🪙 Trial credits: `{trial}`\n"
        "📅 Premium until: `{premium}`\n"
        "🔄 Reset: `{reset}`"
    ),
    "stats.text": (
        "🧠 System Stats\n"
        "━━━━━━━━━━━━━━\n"
        "• CPU: {cpu}%\n"
        "• RAM: {ram}%\n"
        "• Active: {active}\n"
        "• Queue: {queued}\n"
        "• Version: {version}"
    ),

    # Proxy
    "proxy.help": "🧩 Proxy manager\n\nUse:\n• /proxy http://host:port\n• /proxy (to view/remove)",
    "proxy.current": "🧩 Current proxy: `{proxy}`",
    "proxy.none": "🧩 No proxy set.",
    "proxy.set_ok": "✅ Proxy saved: `{proxy}`",
    "proxy.removed": "✅ Proxy removed.",
}

HOT: Dict[str, Template] = {
    # Only overrides; everything else falls back to cold automatically
    "access.group_only": [
        "❌ Not here 😈\n✅ Allowed group: `{group_id}`",
        "🚫 Group-only zone.\n✅ Use: `{group_id}` 🔥",
    ],
    "system.start": [
        "🔥 Bot alive.\nVersion: `{version}` | Theme: `{theme}`\nType /help.",
        "😈 Online.\nVER `{version}` • THEME `{theme}`\n/use /help",
    ],
    "system.theme_set": [
        "✅ Theme switched to **{theme}** 🔥",
        "✅ UI changed → **{theme}** 😈",
    ],
    "playlist.invalid": [
        "❌ That playlist is cooked 💀 Send a real M3U/M3U8.",
        "❌ Invalid playlist. Fix it and try again 😤",
    ],
    "limits.daily_exceeded": [
        "⛔ Limit hit. Reset at `{reset}`. Don’t spam 😌",
        "🚫 Daily quota finished. Come back after `{reset}` 🌿",
    ],
    "proxy.set_ok": [
        "✅ Proxy stored `{proxy}` 🕶️",
        "✅ Proxy saved. `{proxy}` 🔥",
    ],
    "proxy.removed": [
        "✅ Proxy removed ❌",
        "✅ Proxy nuked 💥",
    ],
}

DARK: Dict[str, Template] = {
    "access.group_only": [
        "⛔ ACCESS DENIED.\nALLOWED GROUP: `{group_id}`",
        "⛔ RESTRICTED.\nUSE GROUP: `{group_id}`",
    ],
    "system.start": [
        "🕳️ ONLINE.\nVER `{version}` | THEME `{theme}`\n/use /help",
        "SYSTEM ONLINE.\nVERSION `{version}` • THEME `{theme}`",
    ],
    "system.theme_set": [
        "✅ THEME → `{theme}`",
        "✅ MODE SET: `{theme}`",
    ],
    "playlist.invalid": [
        "⛔ INVALID PLAYLIST INPUT.",
        "⛔ PARSE FAILED. PROVIDE VALID M3U.",
    ],
    "limits.daily_exceeded": [
        "⛔ DAILY LIMIT. RESET `{reset}`",
        "⛔ QUOTA EXCEEDED. RESET `{reset}`",
    ],
    "proxy.set_ok": [
        "✅ PROXY SAVED `{proxy}`",
        "✅ PROXY STORED `{proxy}`",
    ],
    "proxy.removed": [
        "✅ PROXY REMOVED",
        "✅ PROXY CLEARED",
    ],
}

Msg.MESSAGES = {
    "cold": COLD,
    "hot": HOT,
    "dark": DARK,
}
