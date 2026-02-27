# bot/utils/ffmpeg_runner.py

"""
FFmpeg recording engine.

Goals:
- Record live HLS/M3U8 (or any ffmpeg input)
- No re-encode: use -c copy
- Support picking specific video/audio streams (multi-audio)
- Segment output into .mkv parts (Telegram-friendly, ~<2GB)
- Async-compatible (for python-telegram-bot v20+)
- Progress reporting callback
- Done/error callbacks
- One active session per user_id (engine doesn’t handle per-user concurrency more than that)

Public API:

    async def start_recording(
        user_id: int,
        link: str,
        filename_base: str,
        duration_seconds: int | None,
        quality: object,
        audio: object,
        progress_callback,
        done_callback,
        error_callback,
    ) -> None

    async def stop_recording(user_id: int) -> None

The engine expects higher-level code to:

- Enforce limits/concurrency (limits.py)
- Manage mapping from "quality"/"audio" selections to stream indexes.
  For convenience, this module accepts quality/audio as dicts
  that may contain 'stream_index' and 'label'.

Callbacks:

    progress_callback(
        user_id: int,
        filename_base: str,
        elapsed_seconds: float,
        bytes_written: int,
        bitrate_mbps: float | None,
        percent: float | None,
    )

    done_callback(
        user_id: int,
        filename_base: str,
        output_dir: pathlib.Path,
        parts: list[pathlib.Path],
        elapsed_seconds: float,
    )

    error_callback(
        user_id: int,
        filename_base: str,
        message: str,
    )
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..config import (
    DOWNLOADS_DIR,
    OUTPUT_EXTENSION,
    TELEGRAM_MAX_FILE_SIZE,
    PROGRESS_UPDATE_INTERVAL,
    DEBUG_SHOW_FFMPEG_CMD,
    MIN_VALID_DURATION_SECONDS,
)


@dataclass
class RecordingSession:
    user_id: int
    url: str
    filename_base: str
    output_dir: Path
    duration_seconds: Optional[int]          # None or 0 => unlimited
    quality: Any                             # could be str or dict with stream_index/label
    audio: Any                               # same as above
    progress_callback: Optional[Callable[..., Any]]
    done_callback: Optional[Callable[..., Any]]
    error_callback: Optional[Callable[..., Any]]
    proc: Optional[asyncio.subprocess.Process] = None
    task: Optional[asyncio.Task] = None
    start_time: float = 0.0
    stop_requested: bool = False
    parts: List[Path] = field(default_factory=list)


# user_id -> RecordingSession
_sessions: Dict[int, RecordingSession] = {}


# =========================
# Helper utilities
# =========================

def _get_stream_index(info: Any, default: str) -> str:
    """
    Extract a ffmpeg stream index string from `info`.

    If `info` is a dict with 'stream_index', return that.
    Otherwise return default (e.g. '0:v:0' or '0:a:0').
    """
    if isinstance(info, dict):
        idx = info.get("stream_index")
        if idx is not None:
            return f"0:{idx}"
    # default mapping: first video/audio
    return default


def _get_label(info: Any, fallback: str) -> str:
    """
    Extract human label from quality/audio info dict, with fallback.
    """
    if isinstance(info, dict):
        return str(
            info.get("label")
            or info.get("id")
            or info.get("quality")
            or info.get("name")
            or fallback
        )
    if isinstance(info, str):
        return info
    return fallback


async def _maybe_await(cb: Optional[Callable[..., Any]], *args, **kwargs):
    """
    Call callback that may be sync or async.
    """
    if cb is None:
        return
    result = cb(*args, **kwargs)
    if asyncio.iscoroutine(result):
        await result


def _choose_segment_time(duration_seconds: Optional[int]) -> int:
    """
    Choose a segment duration (seconds) for ffmpeg -segment_time.

    - If finite duration, split roughly into ~6–10 segments.
    - Otherwise, default to 15 minutes.
    """
    if duration_seconds and duration_seconds > 0:
        # make 6–10 segments
        seg = max(60, duration_seconds // 8)
        return min(seg, 1800)  # cap at 30 min
    # Unlimited – pick 15 minutes
    return 900


def _list_parts(output_dir: Path, filename_base: str) -> List[Path]:
    """
    List segment files for this recording, sorted.
    """
    pattern = f"{filename_base}_part*.mkv"
    return sorted(output_dir.glob(pattern))


# =========================
# Internal worker
# =========================

async def _record_worker(session: RecordingSession) -> None:
    """
    Internal background task that launches ffmpeg, monitors progress,
    and calls callbacks.
    """
    user_id = session.user_id
    url = session.url
    out_dir = session.output_dir
    base = session.filename_base
    duration = session.duration_seconds

    out_dir.mkdir(parents=True, exist_ok=True)

    # Output path pattern: downloads/<user_or_global>/<base>_part%03d.mkv
    out_pattern = out_dir / f"{base}_part%03d{OUTPUT_EXTENSION}"

    video_map = _get_stream_index(session.quality, "0:v:0")
    audio_map = _get_stream_index(session.audio, "0:a:0")

    segment_time = _choose_segment_time(duration)

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "10",
        "-i", url,
        "-map", video_map,
        "-map", audio_map,
        "-c:v", "copy",
        "-c:a", "copy",
        "-f", "segment",
        "-segment_time", str(segment_time),
        "-reset_timestamps", "1",
        "-strftime", "0",
        str(out_pattern),
    ]

    # Limit by duration if provided
    if duration and duration > 0:
        # Use -t on input side (after -i)
        # We'll insert it just before mapping to keep order simple.
        base_cmd = cmd[:8]  # up to "-i", url
        rest_cmd = cmd[8:]
        cmd = base_cmd + ["-t", str(duration)] + rest_cmd

    if DEBUG_SHOW_FFMPEG_CMD:
        print(f"[ffmpeg_runner] Starting ffmpeg for user {user_id}:")
        print(" ", " ".join(map(str, cmd)))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        await _maybe_await(
            session.error_callback,
            user_id,
            base,
            "ffmpeg not found on system PATH.",
        )
        return
    except Exception as e:
        await _maybe_await(
            session.error_callback,
            user_id,
            base,
            f"Failed to start ffmpeg: {e}",
        )
        return

    session.proc = proc
    session.start_time = time.time()

    last_bytes = 0
    last_time = session.start_time

    try:
        while True:
            # Check for stop request
            if session.stop_requested:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
                break

            # Check if ffmpeg finished on its own
            if proc.returncode is not None:
                break

            # Progress snapshot
            now = time.time()
            elapsed = now - session.start_time
            parts = _list_parts(out_dir, base)
            session.parts = parts

            bytes_written = sum(p.stat().st_size for p in parts if p.exists())
            dt = now - last_time if now > last_time else 0.001
            dbytes = bytes_written - last_bytes
            bitrate_mbps = (dbytes * 8 / dt / 1e6) if dbytes > 0 else 0.0

            last_time = now
            last_bytes = bytes_written

            percent = None
            if duration and duration > 0:
                percent = max(0.0, min(100.0, (elapsed / duration) * 100.0))

            await _maybe_await(
                session.progress_callback,
                user_id,
                base,
                elapsed,
                bytes_written,
                bitrate_mbps,
                percent,
            )

            await asyncio.sleep(PROGRESS_UPDATE_INTERVAL)

        # Ensure ffmpeg is done
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                proc.kill()

        end_time = time.time()
        total_elapsed = end_time - session.start_time
        parts = _list_parts(out_dir, base)
        session.parts = parts

        # Minimal sanity check: skip "recording" shorter than MIN_VALID_DURATION_SECONDS
        if total_elapsed < MIN_VALID_DURATION_SECONDS and not session.stop_requested:
            await _maybe_await(
                session.error_callback,
                user_id,
                base,
                f"Recording too short (<{MIN_VALID_DURATION_SECONDS}s). Stream may have failed.",
            )
            return

        # Done callback
        await _maybe_await(
            session.done_callback,
            user_id,
            base,
            out_dir,
            parts,
            total_elapsed,
        )

    finally:
        # Clean up session from registry
        _sessions.pop(user_id, None)


# =========================
# Public API
# =========================

async def start_recording(
    user_id: int,
    link: str,
    filename_base: str,
    duration_seconds: Optional[int],
    quality: Any,
    audio: Any,
    progress_callback: Optional[Callable[..., Any]],
    done_callback: Optional[Callable[..., Any]],
    error_callback: Optional[Callable[..., Any]],
) -> None:
    """
    Start a recording session for this user.

    Parameters
    ----------
    user_id : int
        Telegram user id (engine uses this as session key).
    link : str
        Stream URL (HLS/M3U8 or any ffmpeg-supported).
    filename_base : str
        Base name for output files, WITHOUT extension.
        Files will look like:
            downloads/<...>/<filename_base>_part001.mkv
    duration_seconds : int | None
        Desired recording length in seconds.
        None or <=0 => unlimited (till stopped).
    quality : object
        Selected quality info; usually a dict from probe_stream(), e.g.:
            {"id": "1080p", "label": "1080p (H.264)", "stream_index": 2}
        If no stream_index given, defaults to first video stream (0:v:0).
    audio : object
        Selected audio info; same idea as quality.
        If no stream_index given, defaults to 0:a:0.
    progress_callback : callable | None
        Called every PROGRESS_UPDATE_INTERVAL seconds:
            progress_callback(
                user_id,
                filename_base,
                elapsed_seconds,
                bytes_written,
                bitrate_mbps,
                percent,
            )
    done_callback : callable | None
        Called when recording finishes normally:
            done_callback(
                user_id,
                filename_base,
                output_dir,
                parts,
                elapsed_seconds,
            )
    error_callback : callable | None
        Called on serious errors:
            error_callback(
                user_id,
                filename_base,
                message,
            )

    Notes
    -----
    - Engine enforces only *one* session per user_id.
      Higher-level code enforces per-user and global concurrency.
    """
    if user_id in _sessions:
        # Already recording for this user
        await _maybe_await(
            error_callback,
            user_id,
            filename_base,
            "Recording already active for this user.",
        )
        return

    # Per-user directory under downloads for clarity
    user_dir = DOWNLOADS_DIR / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)

    session = RecordingSession(
        user_id=user_id,
        url=link,
        filename_base=filename_base,
        output_dir=user_dir,
        duration_seconds=duration_seconds,
        quality=quality,
        audio=audio,
        progress_callback=progress_callback,
        done_callback=done_callback,
        error_callback=error_callback,
    )

    _sessions[user_id] = session

    # Launch background task
    task = asyncio.create_task(_record_worker(session))
    session.task = task


async def stop_recording(user_id: int) -> None:
    """
    Request stop for a recording session.

    - Sets stop_requested flag.
    - Attempts to terminate ffmpeg.
    - Does not remove files; uploader should handle them.

    Safe to call even if no active session.
    """
    session = _sessions.get(user_id)
    if not session:
        return

    session.stop_requested = True

    proc = session.proc
    if proc and proc.returncode is None:
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
        except ProcessLookupError:
            pass

    # Session will be cleaned up in _record_worker finally-block