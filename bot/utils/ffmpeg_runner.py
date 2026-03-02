# bot/utils/ffmpeg_runner.py
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..config import (
    DOWNLOAD_DIR,
    OUTPUT_CONTAINER,
    PART_MAX_BYTES,
    PROGRESS_EDIT_EVERY_SEC,
    FFMPEG_BIN,
)

# Safety defaults (no dependency on config)
MIN_VALID_DURATION_SECONDS = 8


@dataclass
class RecordingSession:
    user_id: int
    url: str
    filename_base: str
    output_dir: Path
    duration_seconds: Optional[int]          # None or 0 => unlimited
    quality: Any                             # dict/str/int; may contain stream_index/label
    audio: Any                               # dict/str/int; may contain stream_index/label
    progress_callback: Optional[Callable[..., Any]]
    done_callback: Optional[Callable[..., Any]]
    error_callback: Optional[Callable[..., Any]]
    proc: Optional[asyncio.subprocess.Process] = None
    task: Optional[asyncio.Task] = None
    start_time: float = 0.0
    stop_requested: bool = False
    parts: List[Path] = field(default_factory=list)


_sessions: Dict[int, RecordingSession] = {}


# =========================
# Helpers
# =========================

def _get_stream_spec(info: Any, default: str) -> str:
    """
    Accepts:
      - dict with stream_index: int or str
      - str like "v:0", "a:0", "0:v:0"
      - int -> treated as "0:<int>" (ffmpeg stream index)
    Returns a valid -map spec.
    """
    idx = None
    if isinstance(info, dict):
        idx = info.get("stream_index")
    else:
        idx = info

    if idx is None:
        return default

    # int -> 0:<n>
    if isinstance(idx, int):
        return f"0:{idx}"

    s = str(idx).strip()
    if not s:
        return default

    # already has input prefix "0:" or "1:" etc
    if s[0].isdigit() and ":" in s:
        return s

    # "v:0" or "a:0" -> "0:v:0"
    if ":" in s:
        return f"0:{s}"

    # numeric in string -> "0:<n>"
    try:
        n = int(s)
        return f"0:{n}"
    except Exception:
        return default


def _choose_segment_time(duration_seconds: Optional[int]) -> int:
    """
    Time-based segmentation only (ffmpeg segment muxer).
    If duration is finite -> ~8 segments.
    Else default 15 minutes.
    """
    if duration_seconds and duration_seconds > 0:
        seg = max(60, duration_seconds // 8)
        return min(seg, 1800)  # <= 30 min
    return 900  # 15 min


def _list_parts(output_dir: Path, filename_base: str) -> List[Path]:
    """
    Parts look like: <base>_part001.<container>
    """
    pattern = f"{filename_base}_part*.{OUTPUT_CONTAINER}"
    return sorted(output_dir.glob(pattern))


async def _maybe_await(cb: Optional[Callable[..., Any]], *args, **kwargs):
    if cb is None:
        return
    res = cb(*args, **kwargs)
    if asyncio.iscoroutine(res):
        await res


# =========================
# Worker
# =========================

async def _record_worker(session: RecordingSession) -> None:
    user_id = session.user_id
    url = session.url
    out_dir = session.output_dir
    base = session.filename_base
    duration = session.duration_seconds if session.duration_seconds and session.duration_seconds > 0 else None

    out_dir.mkdir(parents=True, exist_ok=True)

    # Output pattern
    out_pattern = out_dir / f"{base}_part%03d.{OUTPUT_CONTAINER}"

    vmap = _get_stream_spec(session.quality, "0:v:0")
    amap = _get_stream_spec(session.audio, "0:a:0")

    segment_time = _choose_segment_time(duration)

    cmd = [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel", "warning",

        # HLS stability
        "-user_agent", "Mozilla/5.0",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_at_eof", "1",
        "-reconnect_delay_max", "10",
        "-rw_timeout", "15000000",

        "-i", url,
    ]

    # Limit by duration if provided
    if duration:
        cmd += ["-t", str(int(duration))]

    cmd += [
        "-map", vmap,
        "-map", amap,

        "-c", "copy",

        "-f", "segment",
        "-segment_time", str(int(segment_time)),
        "-reset_timestamps", "1",

        # avoid huge parts when bitrate spikes a lot (best effort)
        "-fs", str(int(PART_MAX_BYTES)),

        str(out_pattern),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,  # avoid stderr pipe deadlock
        )
    except FileNotFoundError:
        await _maybe_await(session.error_callback, user_id, base, "ffmpeg not found on PATH.")
        return
    except Exception as e:
        await _maybe_await(session.error_callback, user_id, base, f"Failed to start ffmpeg: {e}")
        return

    session.proc = proc
    session.start_time = time.time()

    last_bytes = 0
    last_time = session.start_time

    try:
        while True:
            if session.stop_requested:
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                break

            if proc.returncode is not None:
                break

            now = time.time()
            elapsed = now - session.start_time

            parts = _list_parts(out_dir, base)
            session.parts = parts

            bytes_written = 0
            for p in parts:
                try:
                    bytes_written += p.stat().st_size
                except Exception:
                    pass

            dt = max(0.001, now - last_time)
            dbytes = max(0, bytes_written - last_bytes)
            bitrate_mbps = (dbytes * 8 / dt / 1e6) if dbytes > 0 else 0.0

            last_time = now
            last_bytes = bytes_written

            percent = None
            if duration:
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

            await asyncio.sleep(float(PROGRESS_EDIT_EVERY_SEC))

        # Ensure ffmpeg ended
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass

        total_elapsed = time.time() - session.start_time
        parts = _list_parts(out_dir, base)
        session.parts = parts

        # sanity check
        if total_elapsed < MIN_VALID_DURATION_SECONDS and not session.stop_requested:
            await _maybe_await(
                session.error_callback,
                user_id,
                base,
                f"Recording too short (<{MIN_VALID_DURATION_SECONDS}s). Stream may have failed.",
            )
            return

        await _maybe_await(session.done_callback, user_id, base, out_dir, parts, total_elapsed)

    finally:
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
    if user_id in _sessions:
        await _maybe_await(error_callback, user_id, filename_base, "Recording already active for this user.")
        return

    user_dir = (DOWNLOAD_DIR / str(user_id))
    user_dir.mkdir(parents=True, exist_ok=True)

    session = RecordingSession(
        user_id=user_id,
        url=link,
        filename_base=filename_base,
        output_dir=user_dir,
        duration_seconds=duration_seconds if (duration_seconds and duration_seconds > 0) else None,
        quality=quality,
        audio=audio,
        progress_callback=progress_callback,
        done_callback=done_callback,
        error_callback=error_callback,
    )
    _sessions[user_id] = session
    session.task = asyncio.create_task(_record_worker(session))


async def stop_recording(user_id: int) -> None:
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
        except Exception:
            pass
