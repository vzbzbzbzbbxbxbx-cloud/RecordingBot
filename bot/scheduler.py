# bot/scheduler.py
from __future__ import annotations

import datetime as _dt
import re
from zoneinfo import ZoneInfo
from typing import Optional

from .config import DAILY_RESET_TZ

_TZ = ZoneInfo(DAILY_RESET_TZ)

def parse_run_time(s: str) -> Optional[_dt.datetime]:
    """
    Accepts:
      - HH:MM (today or next day if already passed)
      - YYYY-MM-DD HH:MM
    Returns timezone-aware datetime (Asia/Dhaka by default).
    """
    s = (s or "").strip()
    if not s:
        return None

    # YYYY-MM-DD HH:MM
    m = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})$", s)
    if m:
        d = _dt.date.fromisoformat(m.group(1))
        hh = int(m.group(2))
        mm = int(m.group(3))
        return _dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=_TZ)

    # HH:MM
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if m:
        now = _dt.datetime.now(_TZ)
        hh = int(m.group(1))
        mm = int(m.group(2))
        dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if dt <= now:
            dt = dt + _dt.timedelta(days=1)
        return dt

    return None

def parse_duration_hms(s: str) -> Optional[int]:
    """
    HH:MM:SS -> seconds
    """
    s = (s or "").strip()
    m = re.match(r"^(\d+):(\d{2}):(\d{2})$", s)
    if not m:
        return None
    h = int(m.group(1))
    mi = int(m.group(2))
    sec = int(m.group(3))
    return h * 3600 + mi * 60 + sec
