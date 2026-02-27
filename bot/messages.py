from messages.generated import MESSAGES as GENERATED_MESSAGES
# bot/messages.py
"""
messages.py
Central reply catalogue (theme-aware).

Design goals:
- Keep ALL text replies here (commands, errors, statuses, progress)
- Make it easy to expand to hundreds (800+) of messages without code changes
- Support themes: hot/cold/dark
- Provide safe formatting using .format(**kwargs)

Usage:
    text = Msg.get(theme="cold", key="system.start", user="@name")
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Any

_THEMES = ("hot", "cold", "dark")

def _fallback_theme(theme: str) -> str:
    t = (theme or "cold").lower()
    return t if t in _THEMES else "cold"

@dataclass(frozen=True)
class Msg:
    """
    Message catalogue accessor.

    Keys are "namespaced" strings, e.g.:
        - system.start
        - access.group_only
        - playlist.added_url
        - record.progress
        - upload.progress
        - limits.daily_exceeded
    """
    # Expandable storage:
    # MESSAGES[theme][key] = "template {placeholders}"
    MESSAGES: Dict[str, Dict[str, str]] = None  # type: ignore

    @staticmethod
    def get(theme: str, key: str, **kwargs: Any) -> str:
        theme = _fallback_theme(theme)
        catalog = Msg.MESSAGES.get(theme, {})
        template = catalog.get(key) or Msg.MESSAGES["cold"].get(key) or f"[missing:{key}]"
        try:
            return template.format(**kwargs)
        except Exception:
            # Never crash due to formatting issues
            return template

    @staticmethod
    def exists(theme: str, key: str) -> bool:
        theme = _fallback_theme(theme)
        return key in Msg.MESSAGES.get(theme, {}) or key in Msg.MESSAGES.get("cold", {})

# -------------------------
# Catalogue (expandable)
# -------------------------
Msg.MESSAGES = {
    "cold": {
        # Access
        "access.group_only": "❌ This bot can be used only in the allowed group.\n\n✅ Allowed group: `{group_id}`",

        # System
        "system.start": "✅ Bot online.\n\n• Version: `{version}`\n• Theme: `{theme}`\n\nUse /help to see commands.",
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
        "system.theme_set": "✅ Theme changed to **{theme}**.",

        # Playlist
        "playlist.added_url": "✅ Playlist saved from URL.\n• Channels: {count}\n• Auto refresh: every {refresh}s",
        "playlist.added_file": "✅ Playlist saved from file.\n• Channels: {count}",
        "playlist.invalid": "❌ Could not parse playlist. Make sure it's a valid M3U / M3U8.",
        "playlist.none": "❌ No playlist found. Use /playlist and reply to a playlist file or send a URL.",
        "playlist.refresh_ok": "✅ Playlist refreshed.\n• Channels: {count}",

        # Channels
        "channel.header": "📺 Available channels ({count})",
        "channel.item": "• {idx}. `{name}`",
        "channel.none": "❌ No channels found. Add a playlist using /playlist.",

        # Record flow
        "record.queued": "✅ Added to queue.\n• Task: `{task_id}`\n• Source: {source}\n• Duration: {duration}\n• Name: `{filename}`",
        "record.started": "📽️ Recording started.\n• Task: `{task_id}`\n• Source: {source}\n• Duration: {duration}\n• Output: `{filename}`",
        "record.cancelled": "❌ Cancelled.\n• Task: `{task_id}`",
        "record.finished": "✅ Done.\n• Task: `{task_id}`\n• Uploaded parts: {parts}",
        "record.failed": "❌ Recording failed.\n• Task: `{task_id}`\n• Reason: {reason}",

        # Progress
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
    },

    "hot": {
        "access.group_only": "❌ Nope. Group-only zone.\n✅ Allowed group: `{group_id}` 😈",
        "system.start": "🔥 Bot alive.\nVersion: `{version}`\nTheme: `{theme}`\nNow go break the silence. /help",
        "system.help": Msg.MESSAGES["cold"]["system.help"],
        "system.theme_set": "✅ Theme switched to **{theme}** 🔥",
        "playlist.added_url": "✅ Playlist locked in.\nChannels: {count}\nAuto refresh: {refresh}s ⚡",
        "playlist.added_file": "✅ Playlist file eaten.\nChannels: {count} 🍽️",
        "playlist.invalid": "❌ That playlist is cooked 💀 Fix it and try again.",
        "playlist.none": "❌ No playlist found. Use /playlist first 😤",
        "channel.header": "📺 Channels ready ({count})",
        "channel.item": "• {idx}. `{name}`",
        "channel.none": "❌ Zero channels. Add /playlist first 😑",
        "record.queued": "✅ Queued ✅\nTask `{task_id}`\n📽️ {source}\n⏱️ {duration}\n📝 `{filename}`",
        "record.started": "📽️ Recording started ✅\nTask `{task_id}`\nSource: {source}\nName: `{filename}`",
        "record.cancelled": "❌ Cancelled `{task_id}` ✅",
        "record.finished": "✅ Done `{task_id}` — parts: {parts} ❤️",
        "record.failed": "❌ Failed `{task_id}` — {reason}",
        "record.progress": Msg.MESSAGES["cold"]["record.progress"],
        "upload.progress": Msg.MESSAGES["cold"]["upload.progress"],
        "upload.done": "✅ Uploaded `{name}` 🔥",
        "tasks.header": "📌 Tasks (don’t cry) 😎\n━━━━━━━━━━━━━━",
        "tasks.active": "✅ Active ({count})",
        "tasks.queued": "⏳ Queue ({count})",
        "tasks.item": Msg.MESSAGES["cold"]["tasks.item"],
        "limits.need_trial_or_premium": "❌ No premium + no trial = no record 😈 Ask owner.",
        "limits.daily_exceeded": "❌ Limit hit. Touch grass till reset `{reset}` 🌿",
        "limits.trial_no_credits": "❌ Trial finished. Ask owner 😤",
        "status.text": Msg.MESSAGES["cold"]["status.text"],
        "stats.text": Msg.MESSAGES["cold"]["stats.text"],
        "proxy.help": Msg.MESSAGES["cold"]["proxy.help"],
        "proxy.current": Msg.MESSAGES["cold"]["proxy.current"],
        "proxy.none": Msg.MESSAGES["cold"]["proxy.none"],
        "proxy.set_ok": "✅ Proxy stored `{proxy}` 🕶️",
        "proxy.removed": "✅ Proxy nuked ❌",
        "auth.only_owner": "❌ Not for you 😈",
        "auth.ok": Msg.MESSAGES["cold"]["auth.ok"],
        "auth.rm_ok": Msg.MESSAGES["cold"]["auth.rm_ok"],
        "trial.set_ok": Msg.MESSAGES["cold"]["trial.set_ok"],
    },

    "dark": {
        "access.group_only": "⛔ ACCESS DENIED.\nAllowed group: `{group_id}`",
        "system.start": "🕳️ ONLINE.\nVER `{version}` | THEME `{theme}`\n/use /help",
        "system.help": Msg.MESSAGES["cold"]["system.help"],
        "system.theme_set": "✅ THEME → `{theme}`",
        "playlist.added_url": "✅ PLAYLIST STORED.\nCHANNELS: {count}\nREFRESH: {refresh}s",
        "playlist.added_file": "✅ PLAYLIST STORED.\nCHANNELS: {count}",
        "playlist.invalid": "⛔ INVALID PLAYLIST.",
        "playlist.none": "⛔ NO PLAYLIST. USE /playlist.",
        "channel.header": "📺 CHANNELS ({count})",
        "channel.item": "• {idx}. `{name}`",
        "channel.none": "⛔ EMPTY.",
        "record.queued": "✅ QUEUED `{task_id}`\nSRC: {source}\nDUR: {duration}\nNAME: `{filename}`",
        "record.started": "📽️ EXECUTING `{task_id}`\nSRC: {source}\nOUT: `{filename}`",
        "record.cancelled": "⛔ CANCELLED `{task_id}`",
        "record.finished": "✅ COMPLETE `{task_id}` | PARTS {parts}",
        "record.failed": "⛔ FAILED `{task_id}` | {reason}",
        "record.progress": Msg.MESSAGES["cold"]["record.progress"],
        "upload.progress": Msg.MESSAGES["cold"]["upload.progress"],
        "upload.done": "✅ UPLOADED `{name}`",
        "tasks.header": "📌 TASKS\n━━━━━━━━━━━━━━",
        "tasks.active": "✅ ACTIVE ({count})",
        "tasks.queued": "⏳ QUEUE ({count})",
        "tasks.item": Msg.MESSAGES["cold"]["tasks.item"],
        "limits.need_trial_or_premium": "⛔ NOT AUTHORIZED.",
        "limits.daily_exceeded": "⛔ DAILY LIMIT. RESET `{reset}`",
        "limits.trial_no_credits": "⛔ TRIAL=0.",
        "status.text": Msg.MESSAGES["cold"]["status.text"],
        "stats.text": Msg.MESSAGES["cold"]["stats.text"],
        "proxy.help": Msg.MESSAGES["cold"]["proxy.help"],
        "proxy.current": Msg.MESSAGES["cold"]["proxy.current"],
        "proxy.none": Msg.MESSAGES["cold"]["proxy.none"],
        "proxy.set_ok": "✅ PROXY SAVED `{proxy}`",
        "proxy.removed": "✅ PROXY REMOVED",
        "auth.only_owner": "⛔ OWNER ONLY.",
        "auth.ok": Msg.MESSAGES["cold"]["auth.ok"],
        "auth.rm_ok": Msg.MESSAGES["cold"]["auth.rm_ok"],
        "trial.set_ok": Msg.MESSAGES["cold"]["trial.set_ok"],
    },
}
