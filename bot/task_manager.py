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

    source: str
    source_kind: str = "link"     # "link" or "channel"
    duration_sec: int = 0         # 0 => LIVE until cancel
    filename: str = "recording"

    headers: Dict[str, str] = field(default_factory=dict)
    inputs: Any = None

    progress_message_id: Optional[int] = None
    reply_to_message_id: Optional[int] = None

    created_at: float = field(default_factory=time.time)
    state: str = "queued"         # queued/active/done/failed/cancelled
    error: Optional[str] = None


class TaskManager:
    def __init__(self, max_concurrent: int = 3, executor=None, **_kwargs):
        self.max_concurrent = max(1, int(max_concurrent))
        self._sem = asyncio.Semaphore(self.max_concurrent)

        self._queue: asyncio.Queue[RecordingTask] = asyncio.Queue()
        self._active: Dict[str, RecordingTask] = {}
        self._queued: Dict[str, RecordingTask] = {}

        self._workers: List[asyncio.Task] = []
        self._runner: Optional[Callable[[RecordingTask], Awaitable[None]]] = None
        self.executor = executor
        self._closed = False

    def bind_runner(self, runner: Callable[[RecordingTask], Awaitable[None]]) -> None:
        self._runner = runner

    async def start(self, workers: int = 3) -> None:
        if self._workers:
            return
        if not self._runner:
            raise RuntimeError("TaskManager runner not set. Call bind_runner(...) first.")
        w = max(1, int(workers))
        for _ in range(w):
            self._workers.append(asyncio.create_task(self._worker_loop()))

    async def close(self) -> None:
        self._closed = True
        for w in self._workers:
            w.cancel()
        self._workers.clear()

    async def enqueue(self, task: RecordingTask) -> None:
        if self._closed:
            raise RuntimeError("TaskManager is closed")
        self._queued[task.task_id] = task
        await self._queue.put(task)

    def get_active(self) -> List[RecordingTask]:
        return list(self._active.values())

    def get_queued(self) -> List[RecordingTask]:
        return list(self._queued.values())

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
                # IMPORTANT: update this import if your folder name differs
                from .utils.chunk_pipeline import request_stop
                request_stop(task_id)
            except Exception:
                # if your folder is named "untils", change import to:
                # from .untils.chunk_pipeline import request_stop
                pass
            return True

        return False

    async def cancel_user(self, user_id: int) -> int:
        n = 0
        for tid, t in list(self._queued.items()):
            if t.user_id == user_id:
                self._queued.pop(tid, None)
                t.state = "cancelled"
                n += 1

        for tid, t in list(self._active.items()):
            if t.user_id == user_id:
                await self.cancel_task(tid)
                n += 1
        return n

    async def _worker_loop(self) -> None:
        assert self._runner is not None
        while not self._closed:
            task = await self._queue.get()

            # was cancelled while waiting
            if task.task_id not in self._queued:
                self._queue.task_done()
                continue

            async with self._sem:
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
