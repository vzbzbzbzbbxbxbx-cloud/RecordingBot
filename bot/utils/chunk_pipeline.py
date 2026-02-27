# bot/utils/chunk_pipeline.py
from __future__ import annotations

import asyncio
import datetime as _dt
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, Optional, List, Callable

from telegram import Bot

from ..config import DOWNLOAD_DIR, TMP_DIR, PART_MAX_BYTES, OUTPUT_CONTAINER, FFMPEG_BIN
from ..messages import Msg
from ..ui import get_theme
from ..progress import ProgressTracker
from ..limits import add_usage
from ..playlist import maybe_refresh_for_active, resolve_channel
from .uploader import send_video_with_progress
from .probe import media_duration_seconds
from .http import fetch_text
from .hls import is_master_playlist, parse_master

# Stop flags and running processes (by task_id)
_stop: Dict[str, asyncio.Event] = {}
_proc: Dict[str, asyncio.subprocess.Process] = {}

def request_stop(task_id: str) -> None:
    ev = _stop.get(task_id)
    if ev:
        ev.set()
    p = _proc.get(task_id)
    if p and p.returncode is None:
        try:
            p.terminate()
        except ProcessLookupError:
            pass

def _headers_to_ffmpeg(headers: Dict[str, str] | None) -> str:
    if not headers:
        return ""
    # ffmpeg expects CRLF between header lines
    return "".join([f"{k}: {v}\r\n" for k, v in headers.items()])

async def _run_ffmpeg(cmd: List[str]) -> int:
    p = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    return await p.wait()

def _is_vod_playlist(text: str) -> bool:
    return "#EXT-X-ENDLIST" in (text or "")

async def _make_thumb(input_path: Path, thumb_path: Path) -> None:
    # Generate a thumbnail at 1s (or as close as possible)
    cmd = [
        FFMPEG_BIN, "-y",
        "-hide_banner", "-loglevel", "error",
        "-i", str(input_path),
        "-ss", "00:00:01",
        "-frames:v", "1",
        str(thumb_path),
    ]
    try:
        subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=35)
    except Exception:
        # best-effort
        pass

async def _remux_to_mp4(input_path: Path, out_mp4: Path) -> bool:
    cmd = [
        FFMPEG_BIN, "-y",
        "-hide_banner", "-loglevel", "error",
        "-i", str(input_path),
        "-map", "0",
        "-c", "copy",
        "-movflags", "+faststart",
        str(out_mp4),
    ]
    try:
        subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=180)
        return out_mp4.exists() and out_mp4.stat().st_size > 0
    except Exception:
        return False

def _safe_name(name: str) -> str:
    keep = []
    for ch in (name or ""):
        if ch.isalnum() or ch in (" ", "-", "_", ".", "[", "]", "(", ")", "+"):
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep).strip() or "recording"

def _hms(seconds: float) -> str:
    s = int(max(0, seconds))
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}"

@dataclass
class RecordingInputs:
    video_url: str
    audio_urls: List[str]
    headers: Dict[str, str]
    bitrate_bps: Optional[int] = None  # from HLS master BANDWIDTH if known
    master_url: Optional[str] = None   # if originally selected from a master playlist
    variant_label: Optional[str] = None
    audio_choice: Optional[str] = None  # 'ALL' or specific audio id


async def run_recording_task(
    *,
    bot: Bot,
    db,
    task: Any,
    theme_name: str,
) -> None:
    """
    Execute a RecordingTask (from task_manager).
    Writes files in 2GB-ish parts and uploads each part as video with thumbnail.
    """
    theme = get_theme(theme_name)
    stop_event = _stop.setdefault(task.task_id, asyncio.Event())

    # Proxy setting (global)
    proxy_url = await db.get_setting("proxy_url", None)

    # Resolve source URL if needed (playlist channel)
    resolved = {"url": task.source, "headers": task.headers or {}}
    if task.source_kind == "channel":
        ch = await resolve_channel(db, task.user_id, task.source)
        if not ch:
            raise RuntimeError("Channel not found in playlist")
        resolved["url"] = ch["url"]
        resolved["headers"] = ch.get("headers") or {}

    inputs: RecordingInputs = task.inputs  # built earlier

    # Determine part duration heuristic (seconds)
    # Make sure each part stays under 2GB using bitrate if known.
    if inputs.bitrate_bps and inputs.bitrate_bps > 0:
        part_sec = int((PART_MAX_BYTES * 8 * 0.92) / inputs.bitrate_bps)
        part_sec = max(60, min(part_sec, 2 * 3600))  # 1 min .. 2 hr
    else:
        part_sec = 15 * 60  # safe default 15min if bitrate unknown

    # Determine if VOD (so we can use -ss offset to avoid re-downloading)
    is_vod = False
    try:
        pl_text, _ = await fetch_text(inputs.video_url, headers=inputs.headers, proxy=proxy_url)
        is_vod = _is_vod_playlist(pl_text)
    except Exception:
        is_vod = False

    # Prepare output directory
    out_dir = DOWNLOAD_DIR / f"{task.task_id}_{task.user_id}"
    tmp_dir = TMP_DIR / f"{task.task_id}_{task.user_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Progress message (edit)
    progress_msg = task.progress_message_id

    async def edit_record_progress(elapsed_sec: float, total_sec: float, speed_bps: float):
        if not progress_msg:
            return
        percent = (elapsed_sec / total_sec * 100.0) if total_sec > 0 else 0.0
        bar = theme.bar(percent)
        eta = theme.fmt_eta(max(0.0, total_sec - elapsed_sec))
        text = Msg.get(
            theme_name,
            "record.progress",
            task_id=task.task_id,
            filename=task.filename,
            elapsed=_hms(elapsed_sec),
            total=_hms(total_sec),
            bar=bar,
            speed=theme.fmt_speed(speed_bps),
            eta=eta,
        )
        try:
            await bot.edit_message_text(chat_id=task.chat_id, message_id=progress_msg, text=text)
        except Exception:
            pass

    # Run parts
    total = task.duration_sec
    elapsed_total = 0.0
    parts_uploaded = 0
    part_index = 1
    vod_offset = 0.0

    last_size = 0
    last_ts = time.time()
    speed_bps = 0.0

    while elapsed_total < total and not stop_event.is_set():
        # Best-effort playlist refresh for active recordings
        await maybe_refresh_for_active(db, bot, task.user_id, proxy=proxy_url)

        # If this task is based on a saved playlist channel, re-resolve the channel URL each part
        if task.source_kind == "channel":
            ch = await resolve_channel(db, task.user_id, task.source)
            if ch:
                inputs.headers = ch.get("headers") or inputs.headers
                # If original selection came from master, treat current channel URL as master_url
                if inputs.master_url:
                    inputs.master_url = ch.get("url") or inputs.master_url
                else:
                    inputs.video_url = ch.get("url") or inputs.video_url

        # If this recording was selected from a master playlist, refresh variant/audio URLs each part
        if inputs.master_url:
            try:
                mtxt, _ = await fetch_text(inputs.master_url, headers=inputs.headers, proxy=proxy_url)
                if is_master_playlist(mtxt):
                    variants, audios = parse_master(mtxt, base_url=inputs.master_url)
                    if variants:
                        # choose matching label if possible, else highest bandwidth
                        chosen = None
                        if inputs.variant_label:
                            for v in variants:
                                if (v.get("label") or "").lower() == inputs.variant_label.lower():
                                    chosen = v
                                    break
                        if chosen is None:
                            def _bw(v):
                                try:
                                    return int(v.get("bandwidth") or v.get("attrs", {}).get("BANDWIDTH") or 0)
                                except Exception:
                                    return 0
                            chosen = sorted(variants, key=_bw, reverse=True)[0]
                        inputs.video_url = chosen["url"]
                        try:
                            inputs.bitrate_bps = int(chosen.get("bandwidth") or chosen.get("attrs", {}).get("BANDWIDTH") or 0) or inputs.bitrate_bps
                        except Exception:
                            pass
                    if audios:
                        if inputs.audio_choice == "ALL":
                            inputs.audio_urls = [a["url"] for a in audios]
                        elif inputs.audio_choice:
                            match = [a["url"] for a in audios if a["id"] == inputs.audio_choice]
                            inputs.audio_urls = match or inputs.audio_urls
            except Exception:
                pass

        remaining = int(max(1, total - elapsed_total))
        this_part_t = min(part_sec, remaining)

        base = _safe_name(task.filename)
        out_path = out_dir / f"{base}.part{part_index:02d}.{OUTPUT_CONTAINER}"
        thumb_path = tmp_dir / f"{base}.part{part_index:02d}.jpg"
        mp4_path = tmp_dir / f"{base}.part{part_index:02d}.mp4"

        # Build ffmpeg cmd
        cmd: List[str] = [
            FFMPEG_BIN, "-y",
            "-hide_banner", "-loglevel", "error",
            "-reconnect", "1",
            "-reconnect_at_eof", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",
        ]

        if proxy_url:
            cmd += ["-http_proxy", proxy_url]

        # VOD resume
        if is_vod and vod_offset > 0:
            cmd += ["-ss", str(int(vod_offset))]

        # Input 0: video (could include audio but we map only video if we add external audios)
        hdr = _headers_to_ffmpeg(inputs.headers)
        if hdr:
            cmd += ["-headers", hdr]
        cmd += ["-i", inputs.video_url]

        # Extra audio inputs
        for aurl in inputs.audio_urls:
            if proxy_url:
                cmd += ["-http_proxy", proxy_url]
            if hdr:
                cmd += ["-headers", hdr]
            cmd += ["-i", aurl]

        # Duration and size cap
        cmd += ["-t", str(int(this_part_t)), "-fs", str(int(PART_MAX_BYTES))]

        # Mapping
        if inputs.audio_urls:
            cmd += ["-map", "0:v:0"]
            for idx in range(1, 1 + len(inputs.audio_urls)):
                cmd += ["-map", f"{idx}:a:0"]
        else:
            cmd += ["-map", "0"]

        cmd += ["-c", "copy", str(out_path)]

        # Start recording process
        start_ts = time.time()
        p = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _proc[task.task_id] = p

        # Progress loop while ffmpeg runs
        while p.returncode is None and not stop_event.is_set():
            await asyncio.sleep(2.0)
            # compute speed from file growth
            try:
                size = out_path.stat().st_size if out_path.exists() else 0
            except Exception:
                size = 0
            now = time.time()
            dt = max(1e-6, now - last_ts)
            ds = max(0, size - last_size)
            inst = ds / dt
            speed_bps = inst if speed_bps <= 0 else (0.7 * speed_bps + 0.3 * inst)
            last_ts = now
            last_size = size

            # elapsed based on wall clock inside this part
            elapsed_part = now - start_ts
            await edit_record_progress(elapsed_total + min(elapsed_part, this_part_t), total, speed_bps)

        if stop_event.is_set():
            try:
                p.terminate()
            except Exception:
                pass
            break

        rc = await p.wait()

        # Determine actual written duration
        actual_dur = media_duration_seconds(out_path) or float(this_part_t)
        elapsed_total += float(actual_dur)
        vod_offset += float(actual_dur)

        # If ffmpeg died early (network), retry by restarting next loop (it will continue time)
        if rc != 0 and (not out_path.exists() or out_path.stat().st_size < 1024 * 1024):
            # tiny output means failed
            await asyncio.sleep(3)
            continue

        # Generate thumbnail
        await _make_thumb(out_path, thumb_path)

        # Upload with progress
        tracker = ProgressTracker(total=int(out_path.stat().st_size) if out_path.exists() else 1)

        async def upload_edit_loop():
            while True:
                snap = await tracker.snapshot()
                percent = snap["percent"]
                bar = theme.bar(percent)
                text = Msg.get(
                    theme_name,
                    "upload.progress",
                    task_id=task.task_id,
                    part=f"{part_index}",
                    bar=bar,
                    percent=int(percent),
                    speed=theme.fmt_speed(snap["speed_bps"]),
                    eta=theme.fmt_eta(snap["eta_sec"]),
                )
                try:
                    await bot.edit_message_text(chat_id=task.chat_id, message_id=progress_msg, text=text)
                except Exception:
                    pass
                if snap["done"]:
                    break
                await asyncio.sleep(2.0)

        loop_task = asyncio.create_task(upload_edit_loop())

        caption = f"✅ {base} (part {part_index})"
        ok = False
        try:
            await send_video_with_progress(
                chat_id=task.chat_id,
                video_path=out_path,
                thumb_path=thumb_path if thumb_path.exists() else None,
                caption=caption,
                tracker=tracker,
                reply_to_message_id=task.reply_to_message_id,
                supports_streaming=True,
            )
            ok = True
        except Exception:
            # Telegram can reject MKV as video → remux to MP4 and retry
            if await _remux_to_mp4(out_path, mp4_path):
                tracker2 = ProgressTracker(total=int(mp4_path.stat().st_size) if mp4_path.exists() else 1)
                loop_task.cancel()
                loop_task = asyncio.create_task(upload_edit_loop())
                await send_video_with_progress(
                    chat_id=task.chat_id,
                    video_path=mp4_path,
                    thumb_path=thumb_path if thumb_path.exists() else None,
                    caption=caption,
                    tracker=tracker2,
                    reply_to_message_id=task.reply_to_message_id,
                    supports_streaming=True,
                )
                ok = True

        try:
            await loop_task
        except Exception:
            pass

        if ok:
            parts_uploaded += 1
            # Add usage (non-owner)
            await add_usage(db, task.user_id, int(actual_dur))

        # Clean files to save disk
        for pth in (out_path, mp4_path, thumb_path):
            try:
                if pth.exists():
                    pth.unlink()
            except Exception:
                pass

        part_index += 1
        last_size = 0
        last_ts = time.time()
        speed_bps = 0.0

    # cleanup dir
    try:
        shutil.rmtree(out_dir, ignore_errors=True)
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception:
        pass

    # Mark done message
    if progress_msg:
        done_text = Msg.get(theme_name, "record.finished", task_id=task.task_id, parts=str(parts_uploaded))
        try:
            await bot.edit_message_text(chat_id=task.chat_id, message_id=progress_msg, text=done_text)
        except Exception:
            pass

    # Clear process refs
    _proc.pop(task.task_id, None)
    _stop.pop(task.task_id, None)
