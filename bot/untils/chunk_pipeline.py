# bot/utils/chunk_pipeline.py
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, Optional, List

from telegram import Bot

from ..config import DOWNLOAD_DIR, TMP_DIR, PART_MAX_BYTES, OUTPUT_CONTAINER, FFMPEG_BIN
from ..messages import Msg
from ..ui import get_theme
from ..progress import ProgressTracker
from ..playlist import maybe_refresh_for_active, resolve_channel
from .uploader import send_video_with_progress
from .probe import media_duration_seconds
from .http import fetch_text
from .hls import is_master_playlist, parse_master


# Stop flags and running processes (by task_id)
_stop: Dict[str, asyncio.Event] = {}
_proc: Dict[str, asyncio.subprocess.Process] = {}
_stderr_tasks: Dict[str, asyncio.Task] = {}


# ----------------------------
# Controls
# ----------------------------
STALL_SECONDS = 25          # if file size doesn't grow for this long -> restart ffmpeg
RETRY_BACKOFF_BASE = 2.0    # seconds
RETRY_BACKOFF_MAX = 20.0
MIN_GOOD_BYTES = 2 * 1024 * 1024  # 2MB threshold to consider part "usable"


def request_stop(task_id: str) -> None:
    ev = _stop.get(task_id)
    if ev:
        ev.set()

    p = _proc.get(task_id)
    if p and p.returncode is None:
        try:
            p.send_signal(signal.SIGINT)
        except Exception:
            try:
                p.terminate()
            except Exception:
                pass


def _headers_to_ffmpeg(headers: Dict[str, str] | None) -> str:
    if not headers:
        return ""
    # ffmpeg expects CRLF between header lines
    return "".join([f"{k}: {v}\r\n" for k, v in headers.items()])


def _is_vod_playlist(text: str) -> bool:
    return "#EXT-X-ENDLIST" in (text or "")


async def _make_thumb(input_path: Path, thumb_path: Path) -> None:
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
    bitrate_bps: Optional[int] = None
    master_url: Optional[str] = None
    variant_label: Optional[str] = None
    audio_choice: Optional[str] = None  # 'ALL' or audio id


async def _drain_stderr(task_id: str, proc: asyncio.subprocess.Process) -> None:
    """
    CRITICAL: prevent ffmpeg from freezing because stderr PIPE buffer fills.
    We drain it continuously (discard content).
    """
    try:
        if not proc.stderr:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
    except Exception:
        pass


async def _terminate_proc(task_id: str, proc: asyncio.subprocess.Process, timeout: float = 8.0) -> None:
    try:
        if proc.returncode is None:
            try:
                proc.send_signal(signal.SIGINT)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await proc.wait()
        except Exception:
            pass


def _add_input_opts(cmd: List[str], proxy_url: Optional[str], headers: Dict[str, str]) -> None:
    # These options apply to the next -i only.
    if proxy_url:
        cmd += ["-http_proxy", proxy_url]

    # Reconnect / timeouts: helps “drop & resume” instead of hanging forever
    cmd += [
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_at_eof", "1",
        "-reconnect_delay_max", "10",
        "-reconnect_on_network_error", "1",
        "-reconnect_on_http_error", "4xx,5xx",
        "-rw_timeout", "15000000",  # 15s read/write timeout (microseconds)
    ]

    hdr = _headers_to_ffmpeg(headers)
    if hdr:
        cmd += ["-headers", hdr]


async def run_recording_task(
    *,
    bot: Bot,
    db,
    task: Any,
    theme_name: str,
) -> None:
    """
    Execute a RecordingTask:
    - records in parts (2GB cap + time heuristic)
    - auto-restarts on network drop / stalls
    - uploads as playable video w/ thumbnail
    """
    theme = get_theme(theme_name)
    stop_event = _stop.setdefault(task.task_id, asyncio.Event())

    proxy_url = await db.get_setting("proxy_url", None)

    # Resolve source if needed (playlist channel)
    if task.source_kind == "channel":
        ch = await resolve_channel(db, task.user_id, task.source)
        if not ch:
            raise RuntimeError("Channel not found in playlist")
        task.source = ch["url"]
        task.headers = ch.get("headers") or {}

    inputs: RecordingInputs = task.inputs

    # LIVE handling:
    # duration_sec <= 0 means live / infinite until /cancel
    is_live = (task.duration_sec is None) or (int(task.duration_sec) <= 0)
    total_target = float("inf") if is_live else float(task.duration_sec)

    # Part duration heuristic (keeps part <=2GB using bitrate if known)
    if inputs.bitrate_bps and inputs.bitrate_bps > 0:
        part_sec = int((PART_MAX_BYTES * 8 * 0.92) / inputs.bitrate_bps)
        part_sec = max(60, min(part_sec, 2 * 3600))
    else:
        part_sec = 15 * 60

    # Determine VOD (so we can resume with -ss on next part)
    is_vod = False
    try:
        pl_text, _ = await fetch_text(inputs.video_url, headers=inputs.headers, proxy=proxy_url)
        is_vod = _is_vod_playlist(pl_text)
    except Exception:
        is_vod = False

    out_dir = DOWNLOAD_DIR / f"{task.task_id}_{task.user_id}"
    tmp_dir = TMP_DIR / f"{task.task_id}_{task.user_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    progress_msg = task.progress_message_id

    async def edit_record_progress(elapsed_sec: float, speed_bps: float):
        if not progress_msg:
            return

        if is_live:
            pct = None
            eta = ""
            bar = theme.bar(100.0)  # just show full bar for live
            total_txt = "LIVE"
        else:
            pct = (elapsed_sec / total_target * 100.0) if total_target > 0 else 0.0
            bar = theme.bar(pct)
            eta = theme.fmt_eta(max(0.0, total_target - elapsed_sec))
            total_txt = _hms(total_target)

        # Prefer UI-driven progress if theme provides it
        if hasattr(theme, "progress_recording"):
            try:
                text = theme.progress_recording(
                    filename=task.filename,
                    is_live=is_live,
                    bar=bar,
                    percent=pct,
                    speed=theme.fmt_speed(speed_bps),
                    eta=eta,
                    elapsed=_hms(elapsed_sec),
                    total=None if is_live else total_txt,
                )
            except Exception:
                text = Msg.get(
                    theme_name,
                    "record.progress",
                    task_id=task.task_id,
                    filename=task.filename,
                    elapsed=_hms(elapsed_sec),
                    total=total_txt,
                    bar=bar,
                    speed=theme.fmt_speed(speed_bps),
                    eta=eta,
                    pct=int(pct or 0),
                )
        else:
            text = Msg.get(
                theme_name,
                "record.progress",
                task_id=task.task_id,
                filename=task.filename,
                elapsed=_hms(elapsed_sec),
                total=total_txt,
                bar=bar,
                speed=theme.fmt_speed(speed_bps),
                eta=eta,
                pct=int(pct or 0),
            )

        try:
            await bot.edit_message_text(chat_id=task.chat_id, message_id=progress_msg, text=text)
        except Exception:
            pass

    elapsed_total = 0.0
    parts_uploaded = 0
    part_index = 1
    vod_offset = 0.0

    # speed estimation
    last_size = 0
    last_ts = time.time()
    speed_bps = 0.0

    fail_streak = 0

    try:
        while (is_live or elapsed_total < total_target) and not stop_event.is_set():
            await maybe_refresh_for_active(db, bot, task.user_id, proxy=proxy_url)

            # re-resolve channel URL each part
            if task.source_kind == "channel":
                ch = await resolve_channel(db, task.user_id, task.source)
                if ch:
                    inputs.headers = ch.get("headers") or inputs.headers
                    if inputs.master_url:
                        inputs.master_url = ch.get("url") or inputs.master_url
                    else:
                        inputs.video_url = ch.get("url") or inputs.video_url

            # If selected from master playlist: refresh variant/audio each part
            if inputs.master_url:
                try:
                    mtxt, _ = await fetch_text(inputs.master_url, headers=inputs.headers, proxy=proxy_url)
                    if is_master_playlist(mtxt):
                        variants, audios = parse_master(mtxt, base_url=inputs.master_url)
                        if variants:
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
                                inputs.bitrate_bps = int(
                                    chosen.get("bandwidth") or chosen.get("attrs", {}).get("BANDWIDTH") or 0
                                ) or inputs.bitrate_bps
                            except Exception:
                                pass

                        if audios:
                            if inputs.audio_choice == "ALL":
                                inputs.audio_urls = [a["url"] for a in audios]
                            elif inputs.audio_choice:
                                match = [a["url"] for a in audios if a.get("id") == inputs.audio_choice]
                                inputs.audio_urls = match or inputs.audio_urls
                except Exception:
                    pass

            # part duration (live -> always part_sec)
            if is_live:
                this_part_t = part_sec
            else:
                remaining = max(1.0, total_target - elapsed_total)
                this_part_t = min(float(part_sec), float(remaining))

            base = _safe_name(task.filename)
            out_path = out_dir / f"{base}.part{part_index:02d}.{OUTPUT_CONTAINER}"
            thumb_path = tmp_dir / f"{base}.part{part_index:02d}.jpg"
            mp4_path = tmp_dir / f"{base}.part{part_index:02d}.mp4"

            # ffmpeg command
            cmd: List[str] = [
                FFMPEG_BIN, "-y",
                "-hide_banner",
                "-loglevel", "warning",
            ]

            # VOD resume
            if is_vod and vod_offset > 0:
                cmd += ["-ss", str(int(vod_offset))]

            # Input 0: video
            _add_input_opts(cmd, proxy_url, inputs.headers)
            cmd += ["-i", inputs.video_url]

            # Extra audio inputs
            for aurl in inputs.audio_urls:
                _add_input_opts(cmd, proxy_url, inputs.headers)
                cmd += ["-i", aurl]

            # duration and size cap
            cmd += ["-t", str(int(this_part_t)), "-fs", str(int(PART_MAX_BYTES))]

            # mapping
            if inputs.audio_urls:
                cmd += ["-map", "0:v:0"]
                for idx in range(1, 1 + len(inputs.audio_urls)):
                    cmd += ["-map", f"{idx}:a:0"]
            else:
                cmd += ["-map", "0"]

            cmd += ["-c", "copy", str(out_path)]

            # spawn ffmpeg (IMPORTANT: drain stderr)
            start_ts = time.time()
            p = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,   # we WILL drain it (no freeze)
            )
            _proc[task.task_id] = p
            _stderr_tasks[task.task_id] = asyncio.create_task(_drain_stderr(task.task_id, p))

            stall_since: Optional[float] = None

            # progress loop
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

                # stall watchdog (prevents “hang forever”)
                if ds == 0:
                    if stall_since is None:
                        stall_since = now
                    elif (now - stall_since) >= STALL_SECONDS:
                        # stalled: restart ffmpeg
                        await _terminate_proc(task.task_id, p, timeout=6.0)
                        break
                else:
                    stall_since = None

                elapsed_part = min(now - start_ts, this_part_t)
                await edit_record_progress(elapsed_total + elapsed_part, speed_bps)

            if stop_event.is_set():
                await _terminate_proc(task.task_id, p, timeout=6.0)
                break

            rc = await p.wait()

            # cleanup stderr drainer
            t = _stderr_tasks.pop(task.task_id, None)
            if t:
                try:
                    t.cancel()
                except Exception:
                    pass

            # Determine actual duration reliably
            elapsed_wall = min(time.time() - start_ts, this_part_t)
            probed = media_duration_seconds(out_path)
            actual_dur = float(probed) if (probed and probed > 0) else float(elapsed_wall)

            # If ffmpeg died very early + tiny output => retry with backoff
            tiny = (not out_path.exists()) or (out_path.stat().st_size < MIN_GOOD_BYTES)
            if rc != 0 and tiny:
                fail_streak += 1
                backoff = min(RETRY_BACKOFF_MAX, RETRY_BACKOFF_BASE * (1.6 ** min(fail_streak, 8)))
                await asyncio.sleep(backoff)
                # do not advance elapsed_total
                continue

            # We got a usable file (even if rc != 0). Upload it then continue.
            fail_streak = 0
            elapsed_total += actual_dur
            vod_offset += actual_dur

            # thumb
            await _make_thumb(out_path, thumb_path)

            # Upload with progress (FIX: tracker closure bug)
            async def upload_edit_loop(tracker_obj: ProgressTracker):
                while True:
                    snap = await tracker_obj.snapshot()
                    percent = float(snap["percent"])
                    bar = theme.bar(percent)

                    # Prefer theme upload ui
                    if hasattr(theme, "progress_upload"):
                        try:
                            text = theme.progress_upload(
                                part_label=str(part_index),
                                bar=bar,
                                percent=percent,
                                speed=theme.fmt_speed(snap["speed_bps"]),
                                eta=theme.fmt_eta(snap["eta_sec"]),
                            )
                        except Exception:
                            text = Msg.get(
                                theme_name,
                                "upload.progress",
                                task_id=task.task_id,
                                part=str(part_index),
                                bar=bar,
                                percent=int(percent),
                                speed=theme.fmt_speed(snap["speed_bps"]),
                                eta=theme.fmt_eta(snap["eta_sec"]),
                            )
                    else:
                        text = Msg.get(
                            theme_name,
                            "upload.progress",
                            task_id=task.task_id,
                            part=str(part_index),
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

            caption = f"✅ {base} (part {part_index})"
            ok = False

            tracker = ProgressTracker(total=int(out_path.stat().st_size) if out_path.exists() else 1)
            loop_task = asyncio.create_task(upload_edit_loop(tracker))

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
                # Telegram might reject MKV as video -> remux MP4 and retry
                if await _remux_to_mp4(out_path, mp4_path):
                    try:
                        loop_task.cancel()
                    except Exception:
                        pass

                    tracker2 = ProgressTracker(total=int(mp4_path.stat().s
