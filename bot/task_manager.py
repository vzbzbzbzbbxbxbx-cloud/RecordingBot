# bot/task_manager.py
from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Callable, Awaitable, List


def new_task_id(prefix: str = "REC") -> str:
    return f"{prefix}-{int(time.time())}-{secrets.token_hex(3)}"


@dataclass
class RecordingTask:
    task_id: str
    user_id: int
    chat_id: int

    # source
    source: str
    source_kind: str = "url"      # "url" or "channel" (main.py uses "url")
    duration_sec: int = 0         # seconds (0 is allowed by pipeline, but your main.py currently rejects <=0)
    filename: str = "recording"

    # network
    headers: Dict[str, str] = field(default_factory=dict)

    # built earlier (RecordingInputs from chunk_pipeline)
    inputs: Any = None

    # telegram message ids
    progress_message_id: Optional[int] = None
    reply_to_message_id: Optional[int] = None

    # theme required by your executor/run_recording_task usage
    theme_name: str = "cold"

    # runtime
    created_at: float = field(default_factory=time.time)
    state: str = "queued"         # queued/active/done/failed/cancelled
    error: Optional[str] = None


class TaskManager:
    """
    Queue + concurrency controller.

    ✅ Compatible with your current main.py:
      - TaskManager(executor=executor)
      - await tm.start()
      - await tm.stop()
      - await tm.snapshot()
      - await tm.enqueue(task)
      - await tm.cancel_user(user_id)
    """

    def __init__(self, max_concurrent: int = 3, executor: Optional[Callable[[RecordingTask], Awaitable[None]]] = None, **_kwargs):
        self.max_concurrent = max(1, int(max_concurrent))
        self.executor = executor

        # If executor is passed (your main.py does this), treat it as the runner.
        self._runner: Optional[Callable[[RecordingTask], Awaitable[None]]] = executor

        self._sem = asyncio.Semaphore(self.max_concurrent)
        self._queue: asyncio.Queue[RecordingTask] = asyncio.Queue()

        # Keep dicts so we can show /tasks and cancel queued tasks safely
        self._active: Dict[str, RecordingTask] = {}
        self._queued: Dict[str, RecordingTask] = {}

        self._workers: List[asyncio.Task] = []
        self._closed = False

    # -------------------------
    # Runner wiring
    # -------------------------
    def bind_runner(self, runner: Callable[[RecordingTask], Awaitable[None]]) -> None:
        """Optional (not used by your current main.py, but kept for future use)."""
        self._runner = runner

    # -------------------------
    # Lifecycle
    # -------------------------
    async def start(self, workers: int = 3) -> None:
        """
        Start workers. Your main.py calls this in post_init().
        Must NOT crash if executor was provided.
        """
        if self._workers:
            return
        if not self._runner:
            raise RuntimeError("TaskManager runner not set. (Pass executor=... or call bind_runner(...))")

        w = max(1, int(workers))
        for _ in range(w):
            self._workers.append(asyncio.create_task(self._worker_loop()))

    async def close(self) -> None:
        self._closed = True
        for w in self._workers:
            w.cancel()
        self._workers.clear()

        # mark queued tasks as cancelled
        for t in self._queued.values():
            t.state = "cancelled"
        self._queued.clear()

    async def stop(self) -> None:
        """Alias because your main.py calls tm.stop() on shutdown."""
        await self.close()

    # -------------------------
    # Queue API
    # -------------------------
    async def enqueue(self, task: RecordingTask) -> None:
        if self._closed:
            raise RuntimeError("TaskManager is closed")
        self._queued[task.task_id] = task
        await self._queue.put(task)

    # -------------------------
    # Introspection
    # -------------------------
    async def snapshot(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        Your /tasks and /stats call await tm.snapshot()
        """
        def pack(t: RecordingTask) -> Dict[str, Any]:
            return {
                "task_id": t.task_id,
                "user_id": t.user_id,
                "chat_id": t.chat_id,
                "state": t.state,
                "filename": t.filename,
                "source": t.source,
                "source_kind": t.source_kind,
                "duration_sec": t.duration_sec,
                "created_at": t.created_at,
                "theme_name": t.theme_name,
                "error": t.error,
            }

        active = [pack(t) for t in self._active.values()]
        queued = [pack(t) for t in self._queued.values()]
        # stable-ish ordering
        active.sort(key=lambda x: x["created_at"])
        queued.sort(key=lambda x: x["created_at"])
        return {"active": active, "queued": queued}

    # -------------------------
    # Cancellation
    # -------------------------
    async def cancel_task(self, task_id: str) -> bool:
        # queued cancel
        t = self._queued.pop(task_id, None)
        if t:
            t.state = "cancelled"
            return True

        # active cancel (signal pipeline)
        t = self._active.get(task_id)
        if t:
            t.state = "cancelled"
            try:
                # main.py uses .utils.chunk_pipeline
                from .utils.chunk_pipeline import request_stop
                request_stop(task_id)
            except Exception:
                pass
            return True

        return False

    async def cancel_user(self, user_id: int) -> int:
        n = 0

        # cancel queued
        for tid, t in list(self._queued.items()):
            if t.user_id == user_id:
                self._queued.pop(tid, None)
                t.state = "cancelled"
                n += 1

        # cancel active
        for tid, t in list(self._active.items()):
            if t.user_id == user_id:
                ok = await self.cancel_task(tid)
                if ok:
                    n += 1

        return n

    # -------------------------
    # Worker
    # -------------------------
    async def _worker_loop(self) -> None:
        assert self._runner is not None
        while not self._closed:
            task = await self._queue.get()

            # If it was removed/cancelled while waiting, skip
            if task.task_id not in self._queued:
                self._queue.task_done()
                continue

            async with self._sem:
                # move queued -> active
                self._queued.pop(task.task_id, None)
                self._active[task.task_id] = task
                task.state = "active"

                try:
                    await self._runner(task)
                    if task.state != "cancelled":
                        task.state = "done"
                except Exception as e:
                    if task.state != "cancelled":
                        task.state = "failed"
                        task.error = str(e)
                finally:
                    self._active.pop(task.task_id, None)
                    self._queue.task_done()
