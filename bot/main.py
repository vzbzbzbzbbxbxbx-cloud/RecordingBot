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
    return s.lower().startswith(("http://", "https://"))

def _norm_filename(name: str) -> str:
    name = name.strip().replace("\n", " ")
    return name[:120] if len(name) > 120 else name

async def _theme(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    return await ui.get_theme_for_user(context, user_id)

async def _db(context: ContextTypes.DEFAULT_TYPE) -> DB:
    db: DB = context.application.bot_data["db"]
    return db

# pending selections (quality/audio)
# pending_id -> dict
def _pending_store(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Dict[str, Any]]:
    return context.application.bot_data.setdefault("pending", {})

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
    await update.effective_message.reply_text(
        Msg.get(t, "system.start", version=BOT_VERSION, theme=t)
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_access_or_reply(update, context):
        return
    user = update.effective_user
    if not user:
        return
    t = await _theme(context, user.id)
    await update.effective_message.reply_text(Msg.get(t, "system.help"))

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
    await update.effective_message.reply_text(Msg.get(theme_name, "system.theme_set", theme=theme_name))

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

    url = None
    file_id = None

    # /playlist <url>
    if context.args:
        url = context.args[0]

    # reply to url or file
    if msg.reply_to_message:
        r = msg.reply_to_message
        if r.document:
            file_id = r.document.file_id
        elif r.text and _is_url(r.text.strip()):
            url = r.text.strip()

    try:
        if url and _is_url(url):
            count = await save_playlist_from_url(db, user.id, url, proxy=proxy_url)
            await msg.reply_text(Msg.get(t, "playlist.added_url", count=count, refresh=PLAYLIST_REFRESH_SEC))
            return
        if file_id:
            count = await save_playlist_from_file(db, context.bot, user.id, file_id)
            await msg.reply_text(Msg.get(t, "playlist.added_file", count=count))
            return
    except Exception as e:
        logger.exception("playlist save failed")

    await msg.reply_text(Msg.get(t, "playlist.invalid"))

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
        await update.effective_message.reply_text(Msg.get(t, "playlist.none"))
        return
    channels = pl.get("channels", [])
    # show top 40
    take = channels[:40]
    text = [Msg.get(t, "channel.header", count=len(channels))]
    for idx, ch in enumerate(take, 1):
        text.append(Msg.get(t, "channel.item", idx=idx, name=ch.get("name","?")))
    if len(channels) > len(take):
        text.append(f"\n… and {len(channels)-len(take)} more.")
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

    # Parse args with quotes support
    try:
        parts = shlex.split(update.effective_message.text)
    except Exception:
        parts = (update.effective_message.text or "").split()

    if len(parts) < 4:
        await update.effective_message.reply_text(Msg.get(t, "system.help"))
        return

    source = parts[1]
    duration_s = parts[2]
    filename = _norm_filename(parts[3])

    dur_sec = parse_duration_hms(duration_s)
    if dur_sec is None or dur_sec <= 0:
        await update.effective_message.reply_text("❌ Duration must be HH:MM:SS")
        return

    # limits (owner bypass)
    ok, reason = await can_record(db, user.id, dur_sec)
    if not ok and user.id != OWNER_ID:
        if reason == "need_trial_or_premium":
            await update.effective_message.reply_text(Msg.get(t, "limits.need_trial_or_premium"))
            return
        if reason == "trial_no_credits":
            await update.effective_message.reply_text(Msg.get(t, "limits.trial_no_credits"))
            return
        if reason == "daily_exceeded":
            used, rem = await remaining_today(db, user.id)
            tier = await get_tier(db, user.id)
            await update.effective_message.reply_text(
                Msg.get(t, "limits.daily_exceeded", used=fmt_hms(used), limit=fmt_hms(tier.daily_limit_sec or 0), reset=reset_str())
            )
            return

    # Resolve source -> url + headers
    source_kind = "url" if _is_url(source) else "channel"
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
            if not variants:
                # treat as normal if parser didn't find variants
                raise RuntimeError("No variants found")
            pending_id = new_task_id()
            pending = {
                "pending_id": pending_id,
                "user_id": user.id,
                "chat_id": update.effective_chat.id,
                "source_kind": source_kind,
                "source": source,
                "resolved_url": url,
                "headers": headers,
                "duration_sec": dur_sec,
                "filename": filename,
                "variants": variants,
                "audios": audios,
                "selected_variant": None,
                "selected_audios": None,
                "reply_to_message_id": update.effective_message.message_id,
            }
            _pending_store(context)[pending_id] = pending
            m = await update.effective_message.reply_text(
                "📽️ Select quality:",
                reply_markup=quality_keyboard(pending_id, variants),
            )
            pending["message_id"] = m.message_id
            return
    except Exception:
        pass

    # Direct queue (no selection)
    tm: TaskManager = context.application.bot_data["task_manager"]
    task_id = new_task_id()

    # Build inputs
    inputs = RecordingInputs(video_url=url, audio_urls=[], headers=headers, bitrate_bps=None, master_url=None, variant_label=None, audio_choice=None)

    m = await update.effective_message.reply_text(
        Msg.get(t, "record.queued", task_id=task_id, source=source, duration=duration_s, filename=filename)
    )

    task = RecordingTask(
        task_id=task_id,
        user_id=user.id,
        chat_id=update.effective_chat.id,
        source_kind=source_kind,
        source=source,
        duration_sec=dur_sec,
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

    try:
        parts = shlex.split(update.effective_message.text)
    except Exception:
        parts = (update.effective_message.text or "").split()

    if len(parts) < 4:
        await update.effective_message.reply_text("❌ Usage: /schedule <link|\"channel\"> <time> <file_name> [duration]")
        return

    source = parts[1]
    time_str = parts[2]
    filename = _norm_filename(parts[3])
    dur_sec = parse_duration_hms(parts[4]) if len(parts) >= 5 else 3600  # default 1 hour

    run_at = parse_run_time(time_str)
    if not run_at:
        await update.effective_message.reply_text("❌ Time format: HH:MM or YYYY-MM-DD HH:MM")
        return

    schedule_id = new_task_id()
    await db.create_schedule({
        "schedule_id": schedule_id,
        "user_id": user.id,
        "chat_id": update.effective_chat.id,
        "source": source,
        "filename": filename,
        "duration_sec": int(dur_sec),
        "run_at": run_at.astimezone(ZoneInfo("UTC")),
    })

    # schedule job
    when = run_at.astimezone(ZoneInfo("UTC"))
    context.application.job_queue.run_once(
        schedule_job,
        when=when,
        data={"schedule_id": schedule_id},
        name=f"schedule_{schedule_id}",
    )
    await update.effective_message.reply_text(f"✅ Scheduled `{schedule_id}` at `{run_at.strftime('%Y-%m-%d %H:%M %Z')}`")

async def schedule_job(context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    tm: TaskManager = context.application.bot_data["task_manager"]
    bot = context.bot

    schedule_id = context.job.data.get("schedule_id")
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
    source_kind = "url" if _is_url(source) else "channel"
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
                # pick highest bandwidth
                def bw(v):
                    try:
                        return int(v.get("bandwidth") or 0)
                    except Exception:
                        return 0
                best = sorted(variants, key=bw, reverse=True)[0]
                bitrate = bw(best)
                inputs = RecordingInputs(
                    video_url=best["url"],
                    audio_urls=[a["url"] for a in audios] if audios else [],
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
        text=Msg.get(theme_name, "record.queued", task_id=task_id, source=source, duration=fmt_hms(duration_sec), filename=filename),
    )
    task = RecordingTask(
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
    await db.update_schedule(schedule_id, {"status": "queued"})

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_access_or_reply(update, context):
        return
    user = update.effective_user
    if not user:
        return
    tm: TaskManager = context.application.bot_data["task_manager"]
    target_id = user.id

    # Owner can cancel by reply
    if user.id == OWNER_ID and update.effective_message.reply_to_message and update.effective_message.reply_to_message.from_user:
        target_id = update.effective_message.reply_to_message.from_user.id

    n = await tm.cancel_user(target_id)
    t = await _theme(context, user.id)
    await update.effective_message.reply_text(f"✅ Cancelled: {n}")

async def tasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_access_or_reply(update, context):
        return
    user = update.effective_user
    if not user:
        return
    t = await _theme(context, user.id)
    tm: TaskManager = context.application.bot_data["task_manager"]
    snap = await tm.snapshot()

    lines = [Msg.get(t, "tasks.header")]
    lines.append(Msg.get(t, "tasks.active", count=len(snap["active"])))
    for it in snap["active"][:15]:
        lines.append(Msg.get(t, "tasks.item", task_id=it["task_id"], user=it["user_id"], state=it["state"], name=it["filename"]))
    lines.append(Msg.get(t, "tasks.queued", count=len(snap["queued"])))
    for it in snap["queued"][:15]:
        lines.append(Msg.get(t, "tasks.item", task_id=it["task_id"], user=it["user_id"], state=it["state"], name=it["filename"]))
    await update.effective_message.reply_text("\n".join(lines))

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_access_or_reply(update, context):
        return
    user = update.effective_user
    if not user:
        return
    db = await _db(context)
    t = await _theme(context, user.id)
    tier = await get_tier(db, user.id)
    used, rem = await remaining_today(db, user.id)
    lim = tier.daily_limit_sec or 0
    await update.effective_message.reply_text(
        Msg.get(
            t,
            "status.text",
            user_id=str(user.id),
            tier=tier.tier,
            used=fmt_hms(used),
            limit=("∞" if tier.tier == "owner" else fmt_hms(lim)),
            trial=str(tier.trial_credits),
            premium=(tier.premium_until.isoformat() if tier.premium_until else "-"),
            reset=reset_str(),
        )
    )

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await enforce_access_or_reply(update, context):
        return
    user = update.effective_user
    if not user:
        return
    t = await _theme(context, user.id)
    tm: TaskManager = context.application.bot_data["task_manager"]
    snap = await tm.snapshot()
    cpu = psutil.cpu_percent(interval=0.3)
    ram = psutil.virtual_memory().percent
    await update.effective_message.reply_text(
        Msg.get(t, "stats.text", cpu=cpu, ram=ram, active=len(snap["active"]), queued=len(snap["queued"]), version=BOT_VERSION)
    )

# -------------------------
# Proxy (owner)
# -------------------------

async def proxy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    db = await _db(context)
    t = await _theme(context, user.id)

    if user.id != OWNER_ID:
        await update.effective_message.reply_text(Msg.get(t, "auth.only_owner"))
        return

    if context.args:
        px = context.args[0].strip()
        await db.set_setting("proxy_url", px)
        await update.effective_message.reply_text(Msg.get(t, "proxy.set_ok", proxy=px))
        return

    cur = await db.get_setting("proxy_url", None)
    if cur:
        await update.effective_message.reply_text(Msg.get(t, "proxy.current", proxy=cur), reply_markup=proxy_remove_keyboard())
    else:
        await update.effective_message.reply_text(Msg.get(t, "proxy.none"))

# -------------------------
# Auth / trial (owner)
# -------------------------

def _parse_auth_delta(s: str) -> Optional[_dt.timedelta]:
    s = (s or "").strip().lower()
    if not s:
        return None
    if s.endswith("d"):
        return _dt.timedelta(days=int(s[:-1]))
    if s.endswith("h"):
        return _dt.timedelta(hours=int(s[:-1]))
    if s.endswith("m"):
        return _dt.timedelta(minutes=int(s[:-1]))
    return None

async def auth_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    if user.id != OWNER_ID:
        t = await _theme(context, user.id)
        await update.effective_message.reply_text(Msg.get(t, "auth.only_owner"))
        return

    if not update.effective_message.reply_to_message or not update.effective_message.reply_to_message.from_user:
        await update.effective_message.reply_text("❌ Reply to a user message with /auth 1d or /auth 30d")
        return
    if not context.args:
        await update.effective_message.reply_text("❌ /auth 1d or /auth 30d")
        return

    delta = _parse_auth_delta(context.args[0])
    if not delta:
        await update.effective_message.reply_text("❌ invalid duration. Example: 1d, 30d, 12h")
        return

    target = update.effective_message.reply_to_message.from_user.id
    db = await _db(context)
    t = await _theme(context, user.id)
    now = _dt.datetime.utcnow()
    u = await db.get_user(target)
    cur = u.get("premium_until")
    if cur and isinstance(cur, _dt.datetime) and cur > now:
        until = cur + delta
    else:
        until = now + delta
    await db.update_user(target, {"premium_until": until})
    await update.effective_message.reply_text(Msg.get(t, "auth.ok", user_id=str(target), until=until.isoformat()))

async def rm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    if user.id != OWNER_ID:
        t = await _theme(context, user.id)
        await update.effective_message.reply_text(Msg.get(t, "auth.only_owner"))
        return

    if not update.effective_message.reply_to_message or not update.effective_message.reply_to_message.from_user:
        await update.effective_message.reply_text("❌ Reply to a user message with /rm")
        return

    target = update.effective_message.reply_to_message.from_user.id
    db = await _db(context)
    t = await _theme(context, user.id)
    await db.update_user(target, {"premium_until": None})
    await update.effective_message.reply_text(Msg.get(t, "auth.rm_ok", user_id=str(target)))

async def trial_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    if user.id != OWNER_ID:
        t = await _theme(context, user.id)
        await update.effective_message.reply_text(Msg.get(t, "auth.only_owner"))
        return

    if not update.effective_message.reply_to_message or not update.effective_message.reply_to_message.from_user:
        await update.effective_message.reply_text("❌ Reply to a user message with /trial 1|2|3")
        return
    if not context.args:
        await update.effective_message.reply_text("❌ /trial 1|2|3")
        return

    try:
        credits = int(context.args[0])
    except Exception:
        await update.effective_message.reply_text("❌ /trial 1|2|3")
        return
    credits = max(0, min(credits, 100))

    target = update.effective_message.reply_to_message.from_user.id
    db = await _db(context)
    t = await _theme(context, user.id)
    await db.update_user(target, {"trial_credits": credits})
    await update.effective_message.reply_text(Msg.get(t, "trial.set_ok", user_id=str(target), credits=str(credits)))

# -------------------------
# Callback queries (quality/audio/proxy)
# -------------------------

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return
    user = update.effective_user
    if not user:
        return
    db = await _db(context)
    t = await _theme(context, user.id)

    data = q.data
    await q.answer()

    # Proxy remove
    if data == "px|rm":
        if user.id != OWNER_ID:
            return
        await db.set_setting("proxy_url", None)
        try:
            await q.edit_message_text(Msg.get(t, "proxy.removed"))
        except Exception:
            pass
        return

    # Pending selectors
    store = _pending_store(context)

    if data.startswith("c|"):
        pending_id = data.split("|", 1)[1]
        pend = store.get(pending_id)
        if not pend:
            return
        if user.id != pend["user_id"] and user.id != OWNER_ID:
            return
        store.pop(pending_id, None)
        try:
            await q.edit_message_text(Msg.get(t, "record.cancelled", task_id=pending_id))
        except Exception:
            pass
        return

    if data.startswith("q|"):
        _, pending_id, variant_id = data.split("|", 2)
        pend = store.get(pending_id)
        if not pend:
            return
        if user.id != pend["user_id"] and user.id != OWNER_ID:
            return
        # store selected variant
        variants = pend["variants"]
        sel = next((v for v in variants if v["id"] == variant_id), None)
        if not sel:
            return
        pend["selected_variant"] = sel
        audios = pend.get("audios") or []
        if audios:
            try:
                await q.edit_message_text("🎶 Select audio:", reply_markup=audio_keyboard(pending_id, audios))
            except Exception:
                pass
        else:
            await _finalize_pending(context, pending_id, audio_list=None, audio_choice_id=None, q=q)
        return

    if data.startswith("a|"):
        _, pending_id, audio_id = data.split("|", 2)
        pend = store.get(pending_id)
        if not pend:
            return
        if user.id != pend["user_id"] and user.id != OWNER_ID:
            return
        audios = pend.get("audios") or []
        if audio_id == "ALL":
            selected = audios
        else:
            selected = [a for a in audios if a["id"] == audio_id]
        await _finalize_pending(context, pending_id, audio_list=selected, audio_choice_id=('ALL' if audio_id=='ALL' else (selected[0]['id'] if selected else None)), q=q)
        return

async def _finalize_pending(context: ContextTypes.DEFAULT_TYPE, pending_id: str, audio_list, audio_choice_id, q):
    store = _pending_store(context)
    pend = store.get(pending_id)
    if not pend:
        return
    user_id = pend["user_id"]
    chat_id = pend["chat_id"]
    db = await _db(context)
    tm: TaskManager = context.application.bot_data["task_manager"]
    theme_name = await ui.get_theme_for_user(context, user_id)

    variant = pend.get("selected_variant")
    if not variant:
        return

    headers = pend.get("headers") or {}
    # bitrate
    bitrate = None
    try:
        b = variant.get("bandwidth") or variant.get("attrs", {}).get("BANDWIDTH")
        bitrate = int(b) if b else None
    except Exception:
        bitrate = None

    audio_urls = [a["url"] for a in (audio_list or [])]
    inputs = RecordingInputs(video_url=variant["url"], audio_urls=audio_urls, headers=headers, bitrate_bps=bitrate, master_url=pend.get('resolved_url'), variant_label=variant.get('label'), audio_choice=audio_choice_id)

    # Create task and enqueue
    task_id = pending_id
    try:
        await q.edit_message_text(
            Msg.get(theme_name, "record.queued", task_id=task_id, source=pend["source"], duration=fmt_hms(pend["duration_sec"]), filename=pend["filename"])
        )
    except Exception:
        pass

    task = RecordingTask(
        task_id=task_id,
        user_id=user_id,
        chat_id=chat_id,
        source_kind=pend["source_kind"],
        source=pend["source"],
        duration_sec=pend["duration_sec"],
        filename=pend["filename"],
        headers=headers,
        inputs=inputs,
        progress_message_id=pend.get("message_id") or q.message.message_id,
        reply_to_message_id=pend.get("reply_to_message_id"),
        theme_name=theme_name,
    )
    await tm.enqueue(task)
    store.pop(pending_id, None)

# -------------------------
# Startup / background jobs
# -------------------------

async def post_init(app):
    # Connect DB
    db = await DB.connect()
    app.bot_data["db"] = db

    # Task manager
    async def executor(task: RecordingTask):
        await run_recording_task(bot=app.bot, db=db, task=task, theme_name=task.theme_name)

    tm = TaskManager(executor=executor)
    app.bot_data["task_manager"] = tm
    await tm.start()

    # Playlist refresh job (every 5 minutes)
    async def refresh_job(context: ContextTypes.DEFAULT_TYPE):
        proxy_url = await db.get_setting("proxy_url", None)
        try:
            await refresh_all_playlists_job(db, app.bot, proxy=proxy_url)
        except Exception:
            pass

    app.job_queue.run_repeating(refresh_job, interval=PLAYLIST_REFRESH_SEC, first=PLAYLIST_REFRESH_SEC, name="playlist_refresh")

async def post_shutdown(app):
    tm: TaskManager = app.bot_data.get("task_manager")
    if tm:
        await tm.stop()
    db: DB = app.bot_data.get("db")
    if db:
        await db.close()

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set. Export BOT_TOKEN in environment.")

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # commands
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("playlist", playlist_cmd))
    application.add_handler(CommandHandler("channel", channel_cmd))
    application.add_handler(CommandHandler("record", record_cmd))
    application.add_handler(CommandHandler("schedule", schedule_cmd))
    application.add_handler(CommandHandler("cancel", cancel_cmd))
    application.add_handler(CommandHandler("tasks", tasks_cmd))
    application.add_handler(CommandHandler(["status","Status"], status_cmd))
    application.add_handler(CommandHandler(["stats","Stats"], stats_cmd))
    application.add_handler(CommandHandler("proxy", proxy_cmd))
    application.add_handler(CommandHandler("auth", auth_cmd))
    application.add_handler(CommandHandler("rm", rm_cmd))
    application.add_handler(CommandHandler("trial", trial_cmd))
    application.add_handler(CommandHandler("hot", hot_cmd))
    application.add_handler(CommandHandler("cold", cold_cmd))
    application.add_handler(CommandHandler("dark", dark_cmd))

    # callbacks
    application.add_handler(CallbackQueryHandler(callbacks))

    application.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
