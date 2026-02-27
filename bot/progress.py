# bot/progress.py
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class ProgressTracker:
    total: int
    sent: int = 0
    start_ts: float = field(default_factory=time.time)
    last_ts: float = field(default_factory=time.time)
    last_sent: int = 0
    speed_bps: float = 0.0
    eta_sec: float = 0.0
    done: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def update(self, sent: int, done: bool = False) -> None:
        async with self.lock:
            now = time.time()
            delta_t = max(1e-6, now - self.last_ts)
            delta_b = max(0, sent - self.last_sent)
            # EMA-ish
            inst = delta_b / delta_t
            self.speed_bps = inst if self.speed_bps <= 0 else (0.7 * self.speed_bps + 0.3 * inst)
            self.sent = sent
            self.last_sent = sent
            self.last_ts = now
            remain = max(0, self.total - self.sent)
            self.eta_sec = (remain / self.speed_bps) if self.speed_bps > 0 else 0.0
            self.done = done or (self.sent >= self.total)

    async def snapshot(self):
        async with self.lock:
            percent = (self.sent / self.total * 100.0) if self.total > 0 else 0.0
            return {
                "total": self.total,
                "sent": self.sent,
                "percent": percent,
                "speed_bps": self.speed_bps,
                "eta_sec": self.eta_sec,
                "done": self.done,
            }
