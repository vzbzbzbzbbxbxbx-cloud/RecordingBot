# bot/main.py
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import shlex
from typing import Dict, Any, Optional, List

import psutil
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from .config import (
    BOT_TOKEN,
    OWNER_ID,
    GROUP_ID,
    DEFAULT_THEME,
    PLAYLIST_REFRESH_SEC,
    BOT_VERSION,
    DAILY_RESET_TZ,
)
from .db import DB, DBError
from .access import enforce_access_or_reply
from .messages import Msg
from . import ui
from .scheduler import parse_run_time, parse_duration_hms
from .limits import get_tier, remaining_today, reset_str, can_record, fmt_hms
from .playlist import save_playlist_from_url, save_playlist_from_file, refresh_all_playlists_job, resolve_channel
from .task_manager import TaskManager, RecordingTask, new_task_id
from .utils.hls import is_master_playlist, parse_master
from .utils.http import fetch_text
from .utils.chunk_pipeline import run_recording_task, request_stop, RecordingInputs

from .buttons import quality_keyboard, audio_keyboard, proxy_remove_keyboard

logger = logging.getLogger(__name__)

# -------------------------
# Helpers
# -------------------------


def _is_url(s: str) -> bool:
    return (s or "").lower().startswith(("http://", "https://"))


def _norm_filename(name: str) -> str:
    name = (name or "").strip().replace("\n", " ")
    return name[:120] if len(name) > 120 else (name or "recording")


async def _theme(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    return await ui.get_theme_for_user(context, user_id)


async def _db(context: ContextTypes.DEFAULT_TYPE) -> DB:
    # Avoid KeyError explosions on early startup
    db = context.application.bot_data.get("db")
    if not db:
        raise RuntimeError("DB not initialized yet")
    return db


# pending selections (quality/audio)
# pending_id -> dict
def _pending_store(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Dict[str, Any]]:
    return context.application.bot_data.setdefault("pending", {})


def _safe_msg(theme_name: str, key: str, **kwargs: Any) -> str:
    """
    Wrapper for Msg.get that:
    - avoids crashing if Msg.get has 'theme' param name collision
    - fills {theme} placeholder if present, without passing theme=... into kwargs
    """
    # never pass theme kw to avoid collision with Msg.get(theme=...)
    kwargs2 = dict(kwargs)
    kwargs2.pop("theme", None)
    try:
        text = Msg.get(theme_name, key, **kwargs2)
    except TypeError:
        # worst case: older Msg.get signature differences
        text = Msg.get(theme_name, key)

    # Fill template placeholder if messages.py uses {theme}
    if "{theme}" in text:
        text = text.replace("{theme}", str(theme_name))
    return text


async def _tm_snapshot(tm: TaskManager) -> Dict[str, List[Dict[str, Any]]]:
    """
    Support both TaskManager implementations:
    - newer: async snapshot()
    - older: get_active(), get_queued()
    """
    if hasattr(tm, "snapshot"):
        snap = tm.snapshot()
        if asyncio.iscoroutine(snap):
            return await snap
        return snap  # type: ignore[return-value]

    def _to_dict(t: Any) -> Dict[str, Any]:
        return {
            "task_id": getattr(t, "task_id", "?"),
            "user_id": getattr(t, "user_id", "?"),
            "state": getattr(t, "state", "?"),
            "filename": getattr(t, "filename", "?"),
        }

    active: List[Dict[str, Any]] = []
    queued: List[Dict[str, Any]] = []
    try:
        if hasattr(tm, "get_active"):
            active = [_to_dict(x) for x in tm.get_active()]  # type: ignore[attr-defined]
        if hasattr(tm, "get_queued"):
            queued = [_to_dict(x) for x in tm.get_queued()]  # type: ignore[attr-defined]
    except Exception:
        pass

    return {"active": active, "queued": queued}


def _task_with_theme(**kwargs: Any) -> RecordingTask:
    """
    Create RecordingTask while being compatible with dataclass versions
    that may not include theme_name / reply_to_message_id fields.
    """
    theme_name = kwargs.pop("theme_name", DEFAULT_THEME)
    reply_to_message_id = kwargs.pop("reply_to_message_id", None)

    task = RecordingTask(**kwargs)  # type: ignore[arg-type]

    # Attach optional attributes if dataclass doesn't define them
    try:
        setattr(task, "theme_name", theme_name)
    except Exception:
        pass
    try:
        if reply_to_message_id is not None:
            setattr(task, "reply_to_message_id", reply_to_message_id)
    except Exception:
        pass

    return task


def _parse_record_parts(raw_text: str) -> List[str]:
    try:
        return shlex.split(raw_text)
    except Exception:
        return (raw_text or "").split()


# -------------------------
# Commands
# -------------------------


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_access_or_reply(update, context):
        return
    user = update.effective_user
    if not user:
        return

    db = await _db(context)
    await db.ensure_user(user.id)

    t = await _theme(context, user.id)
    await update.effective_message.reply_text(_safe_msg(t, "system.start", version=BOT_VERSION))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_access_or_reply(update, context):
        return
    user = update.effective_user
    if not user:
        return

    t = await _theme(context, user.id)
    await update.effective_message.reply_text(_safe_msg(t, "system.help"))


async def hot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_theme_cmd(update, context, "hot")


async def cold_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_theme_cmd(update, context, "cold")


async def dark_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_theme_cmd(update, context, "dark")


async def _set_theme_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, theme_name: str):
    if not await enforce_access_or_reply(update, context):
        return
    user = update.effective_user
    if not user:
        return

    await ui.set_theme_for_user(context, user.id, theme_name)
    await update.effective_message.reply_text(_safe_msg(theme_name, "system.theme_set"))


# -------------------------
# Playlist / channels
# -------------------------


async def playlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_access_or_reply(update, context):
        return
    user = update.effective_user
    if not user:
        return

    db = await _db(context)
    t = await _theme(context, user.id)
    msg = update.effective_message

    # owner proxy (optional) affects playlist fetch
    proxy_url = await db.get_setting("proxy_url", None)

    url: Optional[str] = None
    file_id: Optional[str] = None

    # /playlist <url>
    if context.args:
        url = context.args[0]

    # reply to url or file
    if msg.reply_to_message:
        r = msg.reply_to_message
        if getattr(r, "document", None):
            file_id = r.document.file_id  # type: ignore[union-attr]
        elif getattr(r, "text", None) and _is_url(r.text.strip()):  # type: ignore[union-attr]
            url = r.text.strip()  # type: ignore[union-attr]

    try:
        if url and _is_url(url):
            count = await save_playlist_from_url(db, user.id, url, proxy=proxy_url)
            await msg.reply_text(_safe_msg(t, "playlist.added_url", count=count, refresh=PLAYLIST_REFRESH_SEC))
            return
        if file_id:
            count = await save_playlist_from_file(db, context.bot, user.id, file_id)
            await msg.reply_text(_safe_msg(t, "playlist.added_file", count=count))
            return
    except Exception:
        logger.exception("playlist save failed")

    await msg.reply_text(_safe_msg(t, "playlist.invalid"))


async def channel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_access_or_reply(update, context):
        return
    user = update.effective_user
    if not user:
        return

    db = await _db(context)
    t = await _theme(context, user.id)

    pl = await db.get_playlist(user.id)
    if not pl or not pl.get("channels"):
        await update.effective_message.reply_text(_safe_msg(t, "playlist.none"))
        return

    channels = pl.get("channels", [])
    take = channels[:40]

    text = [_safe_msg(t, "channel.header", count=len(channels))]
    for idx, ch in enumerate(take, 1):
        text.append(_safe_msg(t, "channel.item", idx=idx, name=ch.get("name", "?")))
    if len(channels) > len(take):
        text.append(f"\n… and {len(channels) - len(take)} more.")

    await update.effective_message.reply_text("\n".join(text))


# -------------------------
# Record / schedule
# -------------------------


async def record_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_access_or_reply(update, context):
        return
    user = update.effective_user
    if not user:
        return

    db = await _db(context)
    t = await _theme(context, user.id)

    parts = _parse_record_parts(update.effective_message.text or "")
    if len(parts) < 4:
        await update.effective_message.reply_text(_safe_msg(t, "system.help"))
        return

    source = parts[1]
    duration_s = parts[2]
    filename = _norm_filename(parts[3])

    dur_sec = parse_duration_hms(duration_s)
    if dur_sec is None:
        await update.effective_message.reply_text("❌ Duration must be HH:MM:SS (use 00:00:00 for LIVE)")
        return

    # LIVE mode requested with 00:00:00
    duration_label = duration_s
    if dur_sec == 0:
        if user.id == OWNER_ID:
            # Chunk pipeline currently expects a finite duration; use large duration and rely on /cancel.
            dur_sec = 7 * 24 * 3600  # 7 days "infinite enough"
            duration_label = "LIVE"
        else:
            used, rem = await remaining_today(db, user.id)
            tier = await get_tier(db, user.id)
            if rem <= 0:
                await update.effective_message.reply_text(
                    _safe_msg(
                        t,
                        "limits.daily_exceeded",
                        used=fmt_hms(used),
                        limit=fmt_hms(tier.daily_limit_sec or 0),
                        reset=reset_str(),
                    )
                )
                return
            dur_sec = int(rem)
            duration_label = f"LIVE (max {fmt_hms(dur_sec)})"

    if dur_sec <= 0:
        await update.effective_message.reply_text("❌ Duration must be HH:MM:SS")
        return

    # limits (owner bypass)
    ok, reason = await can_record(db, user.id, int(dur_sec))
    if not ok and user.id != OWNER_ID:
        if reason == "need_trial_or_premium":
            await update.effective_message.reply_text(_safe_msg(t, "limits.need_trial_or_premium"))
            return
        if reason == "trial_no_credits":
            await update.effective_message.reply_text(_safe_msg(t, "limits.trial_no_credits"))
            return
        if reason == "daily_exceeded":
            used, _rem = await remaining_today(db, user.id)
            tier = await get_tier(db, user.id)
            await update.effective_message.reply_text(
                _safe_msg(
                    t,
                    "limits.daily_exceeded",
                    used=fmt_hms(used),
                    limit=fmt_hms(tier.daily_limit_sec or 0),
                    reset=reset_str(),
                )
            )
            return

    # Resolve source -> url + headers
    source_kind = "link" if _is_url(source) else "channel"
    url = source
    headers: Dict[str, str] = {}

    if source_kind == "channel":
        ch = await resolve_channel(db, user.id, source)
        if not ch:
            await update.effective_message.reply_text("❌ Channel not found. Use /channel to list.")
            return
        url = ch["url"]
        headers = ch.get("headers") or {}

    # Detect master playlist → interactive selection
    proxy_url = await db.get_setting("proxy_url", None)
    try:
        txt, _ = await fetch_text(url, headers=headers, proxy=proxy_url)
        if is_master_playlist(txt):
            variants, audios = parse_master(txt, base_url=url)
            if variants:
                pending_id = new_task_id()
                pend = {
                    "pending_id": pending_id,
                    "user_id": user.id,
                    "chat_id": update.effective_chat.id,
                    "source_kind": source_kind,
                    "source": source,
                    "resolved_url": url,
                    "headers": headers,
                    "duration_sec": int(dur_sec),
                    "duration_label": duration_label,
                    "filename": filename,
                    "variants": variants,
                    "audios": audios or [],
                    "selected_variant": None,
                    "reply_to_message_id": update.effective_message.message_id,
                }
                store = _pending_store(context)
                store[pending_id] = pend

                m = await update.effective_message.reply_text(
                    "📽️ Select quality:",
                    reply_markup=quality_keyboard(pending_id, variants),
                )
                pend["message_id"] = m.message_id
                return
    except Exception:
        pass

    # Direct queue (no selection)
    tm: TaskManager = context.application.bot_data["task_manager"]
    task_id = new_task_id()

    inputs = RecordingInputs(
        video_url=url,
        audio_urls=[],
        headers=headers,
        bitrate_bps=None,
        master_url=None,
        variant_label=None,
        audio_choice=None,
    )

    m = await update.effective_message.reply_text(
        _safe_msg(t, "record.queued", task_id=task_id, source=source, duration=duration_label, filename=filename)
    )

    task = _task_with_theme(
        task_id=task_id,
        user_id=user.id,
        chat_id=update.effective_chat.id,
        source_kind=source_kind,
        source=source,
        duration_sec=int(dur_sec),
        filename=filename,
        headers=headers,
        inputs=inputs,
        progress_message_id=m.message_id,
        reply_to_message_id=update.effective_message.message_id,
        theme_name=t,
    )
    await tm.enqueue(task)


async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_access_or_reply(update, context):
        return
    user = update.effective_user
    if not user:
        return

    db = await _db(context)
    t = await _theme(context, user.id)

    parts = _parse_record_parts(update.effective_message.text or "")
    if len(parts) < 4:
        await update.effective_message.reply_text("❌ Usage: /schedule <link|\"channel\"> <time> <file_name> [duration]")
        return

    source = parts[1]
    time_str = parts[2]
    filename = _norm_filename(parts[3])
    dur_sec = parse_duration_hms(parts[4]) if len(parts) >= 5 else 3600  # default 1 hour
    if dur_sec is None or dur_sec <= 0:
        dur_sec = 3600

    run_at = parse_run_time(time_str)
    if not run_at:
        await update.effective_message.reply_text("❌ Time format: HH:MM or YYYY-MM-DD HH:MM")
        return

    if not context.application.job_queue:
        await update.effective_message.reply_text(
            "❌ JobQueue not available. Install: pip install \"python-telegram-bot[job-queue]\""
        )
        return

    schedule_id = new_task_id()
    await db.create_schedule(
        {
            "schedule_id": schedule_id,
            "user_id": user.id,
            "chat_id": update.effective_chat.id,
            "source": source,
            "filename": filename,
            "duration_sec": int(dur_sec),
            "run_at": run_at.astimezone(ZoneInfo("UTC")),
            "status": "scheduled",
        }
    )

    when = run_at.astimezone(ZoneInfo("UTC"))
    context.application.job_queue.run_once(
        schedule_job,
        when=when,
        data={"schedule_id": schedule_id},
        name=f"schedule_{schedule_id}",
    )

    await update.effective_message.reply_text(f"✅ Scheduled `{schedule_id}` at `{run_at.strftime('%Y-%m-%d %H:%M %Z')}`")


async def schedule_job(context: ContextTypes.DEFAULT_TYPE):
    db: DB = context.application.bot_data["db"]
    tm: TaskManager = context.application.bot_data["task_manager"]
    bot = context.bot

    schedule_id = (context.job.data or {}).get("schedule_id")
    if not schedule_id:
        return

    doc = await db.db["schedules"].find_one({"schedule_id": schedule_id})
    if not doc or doc.get("status") != "scheduled":
        return

    user_id = int(doc["user_id"])
    chat_id = int(doc["chat_id"])
    source = doc["source"]
    filename = doc["filename"]
    duration_sec = int(doc["duration_sec"])

    theme_name = await ui.get_theme_for_user(context, user_id)

    # Resolve source
    source_kind = "link" if _is_url(source) else "channel"
    url = source
    headers: Dict[str, str] = {}
    if source_kind == "channel":
        ch = await resolve_channel(db, user_id, source)
        if not ch:
            await bot.send_message(chat_id=chat_id, text=f"❌ Scheduled failed: channel not found ({source})")
            await db.update_schedule(schedule_id, {"status": "failed", "reason": "channel_not_found"})
            return
        url = ch["url"]
        headers = ch.get("headers") or {}

    # Enforce limits at runtime (owner bypass)
    ok, reason = await can_record(db, user_id, duration_sec)
    if not ok and user_id != OWNER_ID:
        await bot.send_message(chat_id=chat_id, text="❌ Scheduled failed: limits reached.")
        await db.update_schedule(schedule_id, {"status": "failed", "reason": reason})
        return

    # If master playlist: auto pick best quality + ALL audio
    proxy_url = await db.get_setting("proxy_url", None)
    inputs = RecordingInputs(video_url=url, audio_urls=[], headers=headers, bitrate_bps=None, master_url=None, variant_label=None, audio_choice=None)

    try:
        txt, _ = await fetch_text(url, headers=headers, proxy=proxy_url)
        if is_master_playlist(txt):
            variants, audios = parse_master(txt, base_url=url)
            if variants:

                def _bw(v):
                    try:
                        return int(v.get("bandwidth") or v.get("attrs", {}).get("BANDWIDTH") or 0)
                    except Exception:
                        return 0

                best = sorted(variants, key=_bw, reverse=True)[0]
                bitrate = _bw(best)
                inputs = RecordingInputs(
                    video_url=best["url"],
                    audio_urls=[a["url"] for a in (audios or [])] if audios else [],
                    headers=headers,
                    bitrate_bps=bitrate if bitrate > 0 else None,
                    master_url=url,
                    variant_label=best.get("label"),
                    audio_choice="ALL" if audios else None,
                )
    except Exception:
        pass

    task_id = schedule_id  # reuse id
    m = await bot.send_message(
        chat_id=chat_id,
        text=_safe_msg(theme_name, "record.queued", task_id=task_id, source=source, duration=fmt_hms(duration_sec), filename=filename),
    )
    task = _task_with_theme(
        task_id=task_id,
        user_id=user_id,
        chat_id=chat_id,
        source_kind=source_kind,
        source=source,
        duration_sec=duration_sec,
        filename=filename,
        headers=headers,
        inputs=inputs,
        progress_message_id=m.message_id,
        theme_name=theme_name,
    )
    await tm.enqueue(task)
    await db.up
