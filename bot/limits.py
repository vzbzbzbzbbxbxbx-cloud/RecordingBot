# bot/limits.py
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Tuple, Optional

from zoneinfo import ZoneInfo

from .config import (
    OWNER_ID,
    DAILY_RESET_TZ,
    PREMIUM_DAILY_LIMIT_SEC,
    TRIAL_DAILY_LIMIT_SEC,
)
from .db import DB

def _now_tz() -> _dt.datetime:
    return _dt.datetime.now(ZoneInfo(DAILY_RESET_TZ))

def day_key() -> str:
    return _now_tz().date().isoformat()

def reset_str() -> str:
    # Next reset at 23:59 local (we show date/time)
    now = _now_tz()
    reset_dt = now.replace(hour=23, minute=59, second=0, microsecond=0)
    if now > reset_dt:
        reset_dt = reset_dt + _dt.timedelta(days=1)
    return reset_dt.strftime("%Y-%m-%d %H:%M %Z")

def fmt_hms(seconds: int) -> str:
    s = max(0, int(seconds))
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}"

@dataclass
class TierInfo:
    tier: str  # owner/premium/trial/free
    daily_limit_sec: Optional[int]
    premium_until: Optional[_dt.datetime]
    trial_credits: int

async def get_tier(db: DB, user_id: int) -> TierInfo:
    if user_id == OWNER_ID:
        return TierInfo("owner", None, None, 999999)
    u = await db.get_user(user_id)
    premium_until = u.get("premium_until")
    if isinstance(premium_until, str):
        premium_until = None
    now = _dt.datetime.utcnow()
    if premium_until and premium_until > now:
        return TierInfo("premium", PREMIUM_DAILY_LIMIT_SEC, premium_until, int(u.get("trial_credits") or 0))
    credits = int(u.get("trial_credits") or 0)
    if credits > 0:
        return TierInfo("trial", TRIAL_DAILY_LIMIT_SEC, None, credits)
    return TierInfo("free", 0, None, 0)

async def remaining_today(db: DB, user_id: int) -> Tuple[int, int]:
    """
    Returns (used_sec, remaining_sec) based on tier daily cap.
    Owner returns (0, very_large).
    """
    tier = await get_tier(db, user_id)
    if tier.tier == "owner" or tier.daily_limit_sec is None:
        return 0, 10**9
    usage = await db.get_usage(user_id, day_key())
    used = int(usage.get("used_seconds") or 0)
    remaining = max(0, int(tier.daily_limit_sec) - used)
    return used, remaining

async def can_record(db: DB, user_id: int, duration_sec: int) -> Tuple[bool, str]:
    """
    Enforce:
    - owner: always allowed
    - premium: 6h/day
    - trial: 3h/day + credits>0
    - free: not allowed
    """
    tier = await get_tier(db, user_id)
    if tier.tier == "owner":
        return True, "owner"
    if tier.tier == "free":
        return False, "need_trial_or_premium"
    if tier.tier == "trial" and tier.trial_credits <= 0:
        return False, "trial_no_credits"
    used, remaining = await remaining_today(db, user_id)
    if duration_sec > remaining:
        return False, "daily_exceeded"
    return True, "ok"

async def consume_trial_if_needed(db: DB, user_id: int) -> None:
    tier = await get_tier(db, user_id)
    if tier.tier == "trial" and user_id != OWNER_ID:
        await db.update_user(user_id, {"trial_credits": max(0, tier.trial_credits - 1)})

async def add_usage(db: DB, user_id: int, seconds: int) -> None:
    if user_id == OWNER_ID:
        return
    await db.add_usage(user_id, day_key(), int(seconds))
