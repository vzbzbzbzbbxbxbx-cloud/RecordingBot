# bot/management.py

"""
High-level management for:

- Roles (owner / admin / normal / banned)
- Dynamic admin list (+ /add, /rm)
- Banned users (+ /ban, /unban)
- Trial helper command (/trial)
- Playlist storage & selection flow
- System metrics / load monitoring helpers

Backends:
- JSON (default)
- MongoDB (optional, if USE_MONGO=True in config.py)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, List, Optional

from telegram import Update
from telegram.ext import ContextTypes

from .config import (
    OWNER_ID,
    ADMIN_IDS,
    DATABASE_DIR,
    USE_MONGO,
    MONGO_URI,
    MONGO_DB_NAME,
    MONGO_COLLECTIONS,
)
from . import ui
from .limits import check_limits

logger = logging.getLogger(__name__)

# ================
# Mongo setup (optional)
# ================

_USE_MONGO_EFFECTIVE = False
_mongo_client = None
_mongo_db = None
_mongo_admins_coll = None
_mongo_banned_coll = None
_mongo_playlists_coll = None

if USE_MONGO:
    try:
        from pymongo import MongoClient, ASCENDING  # type: ignore

        _mongo_client = MongoClient(MONGO_URI, connect=False)
        _mongo_db = _mongo_client[MONGO_DB_NAME]

        _mongo_admins_coll = _mongo_db[MONGO_COLLECTIONS["admins"]]
        _mongo_banned_coll = _mongo_db[MONGO_COLLECTIONS["banned"]]
        _mongo_playlists_coll = _mongo_db[MONGO_COLLECTIONS["playlists"]]

        # Create indexes
        _mongo_admins_coll.create_index([("user_id", ASCENDING)], unique=True)
        _mongo_banned_coll.create_index([("user_id", ASCENDING)], unique=True)
        _mongo_playlists_coll.create_index([("user_id", ASCENDING)], unique=True)

        _USE_MONGO_EFFECTIVE = True
        print("[management] Using MongoDB backend for admins/banned/playlists.")
    except Exception as e:
        print(f"[management] Mongo initialization failed, falling back to JSON: {e}")
        _USE_MONGO_EFFECTIVE = False


# ================
# JSON files & paths
# ================

ADMIN_FILE = DATABASE_DIR / "admins.json"
BANNED_FILE = DATABASE_DIR / "banned.json"
PLAYLIST_DIR = DATABASE_DIR / "playlists"
PLAYLIST_DIR.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ================
# Admin storage (JSON + Mongo)
# ================

def _json_load_admin_ids() -> List[int]:
    data = _load_json(ADMIN_FILE, {"admins": []})
    admins = data.get("admins") or []
    return [int(x) for x in admins]


def _json_save_admin_ids(ids: List[int]) -> None:
    _save_json(ADMIN_FILE, {"admins": sorted(set(int(x) for x in ids))})


def _mongo_load_admin_ids() -> List[int]:
    if not _USE_MONGO_EFFECTIVE or _mongo_admins_coll is None:
        return _json_load_admin_ids()

    ids: List[int] = []
    try:
        for doc in _mongo_admins_coll.find({}, {"user_id": 1}):
            uid = doc.get("user_id")
            if uid is not None:
                ids.append(int(uid))
    except Exception as e:
        logger.warning("Failed to load admins from Mongo: %s", e)
        return _json_load_admin_ids()
    return ids


def _mongo_save_admin_ids(ids: List[int]) -> None:
    if not _USE_MONGO_EFFECTIVE or _mongo_admins_coll is None:
        _json_save_admin_ids(ids)
        return

    try:
        _mongo_admins_coll.delete_many({})
        if ids:
            docs = [{"user_id": int(uid)} for uid in ids]
            _mongo_admins_coll.insert_many(docs)
    except Exception as e:
        logger.warning("Failed to save admins to Mongo: %s", e)
        _json_save_admin_ids(ids)


def load_dynamic_admins() -> List[int]:
    if _USE_MONGO_EFFECTIVE:
        return _mongo_load_admin_ids()
    return _json_load_admin_ids()


def save_dynamic_admins(ids: List[int]) -> None:
    if _USE_MONGO_EFFECTIVE:
        _mongo_save_admin_ids(ids)
    else:
        _json_save_admin_ids(ids)


# ================
# Banned storage (JSON + Mongo)
# ================

def _json_load_banned() -> List[int]:
    data = _load_json(BANNED_FILE, {"banned": []})
    banned = data.get("banned") or []
    return [int(x) for x in banned]


def _json_save_banned(ids: List[int]) -> None:
    _save_json(BANNED_FILE, {"banned": sorted(set(int(x) for x in ids))})


def _mongo_load_banned() -> List[int]:
    if not _USE_MONGO_EFFECTIVE or _mongo_banned_coll is None:
        return _json_load_banned()

    ids: List[int] = []
    try:
        for doc in _mongo_banned_coll.find({}, {"user_id": 1}):
            uid = doc.get("user_id")
            if uid is not None:
                ids.append(int(uid))
    except Exception as e:
        logger.warning("Failed to load banned users from Mongo: %s", e)
        return _json_load_banned()
    return ids


def _mongo_save_banned(ids: List[int]) -> None:
    if not _USE_MONGO_EFFECTIVE or _mongo_banned_coll is None:
        _json_save_banned(ids)
        return

    try:
        _mongo_banned_coll.delete_many({})
        if ids:
            docs = [{"user_id": int(uid)} for uid in ids]
            _mongo_banned_coll.insert_many(docs)
    except Exception as e:
        logger.warning("Failed to save banned users to Mongo: %s", e)
        _json_save_banned(ids)


def load_banned_users() -> List[int]:
    if _USE_MONGO_EFFECTIVE:
        return _mongo_load_banned()
    return _json_load_banned()


def save_banned_users(ids: List[int]) -> None:
    if _USE_MONGO_EFFECTIVE:
        _mongo_save_banned(ids)
    else:
        _json_save_banned(ids)


# ================
# Role helpers
# ================

def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def is_admin(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    if user_id in ADMIN_IDS:
        return True
    dyn = load_dynamic_admins()
    return user_id in dyn


def is_banned(user_id: int) -> bool:
    banned = load_banned_users()
    return user_id in banned


def get_role(user_id: int) -> str:
    """
    Role resolution with ban check:
      - if banned -> 'banned'
      - if owner -> 'owner'
      - if admin -> 'admin'
      - else -> 'normal'
    """
    if is_banned(user_id):
        return "banned"
    if is_owner(user_id):
        return "owner"
    if is_admin(user_id):
        return "admin"
    return "normal"


# ================
# Admin Commands: /add, /rm, /ban, /unban
# ================

async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /add <user_id>
    Owner only.
    """
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    theme = ui.get_theme(user.id)
    if not is_owner(user.id):
        await msg.reply_text(theme.error("Only owner can add admins."))
        return

    if not context.args:
        await msg.reply_text(theme.error("Usage: /add <user_id>"))
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await msg.reply_text(theme.error("Invalid user_id."))
        return

    dyn = load_dynamic_admins()
    if target_id in dyn or target_id in ADMIN_IDS or target_id == OWNER_ID:
        await msg.reply_text(theme.info("User is already admin/owner."))
        return

    dyn.append(target_id)
    save_dynamic_admins(dyn)

    await msg.reply_text(
        theme.info(f"User {target_id} has been added as admin.")
    )


async def rm_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /rm <user_id>
    Owner only.
    """
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    theme = ui.get_theme(user.id)
    if not is_owner(user.id):
        await msg.reply_text(theme.error("Only owner can remove admins."))
        return

    if not context.args:
        await msg.reply_text(theme.error("Usage: /rm <user_id>"))
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await msg.reply_text(theme.error("Invalid user_id."))
        return

    dyn = load_dynamic_admins()
    if target_id in dyn:
        dyn = [x for x in dyn if x != target_id]
        save_dynamic_admins(dyn)
        await msg.reply_text(theme.info(f"User {target_id} removed from admins."))
    else:
        await msg.reply_text(theme.info("User not found in dynamic admins."))


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /ban <user_id>
    Owner or admin.
    """
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    theme = ui.get_theme(user.id)
    if not is_admin(user.id):
        await msg.reply_text(theme.error("Only owner/admin can ban users."))
        return

    if not context.args:
        await msg.reply_text(theme.error("Usage: /ban <user_id>"))
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await msg.reply_text(theme.error("Invalid user_id."))
        return

    if is_owner(target_id) or is_admin(target_id):
        await msg.reply_text(theme.error("Cannot ban owner/admin."))
        return

    banned = load_banned_users()
    if target_id in banned:
        await msg.reply_text(theme.info("User is already banned."))
        return

    banned.append(target_id)
    save_banned_users(banned)
    await msg.reply_text(theme.info(f"User {target_id} has been banned."))


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /unban <user_id>
    Owner or admin.
    """
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    theme = ui.get_theme(user.id)
    if not is_admin(user.id):
        await msg.reply_text(theme.error("Only owner/admin can unban users."))
        return

    if not context.args:
        await msg.reply_text(theme.error("Usage: /unban <user_id>"))
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await msg.reply_text(theme.error("Invalid user_id."))
        return

    banned = load_banned_users()
    if target_id not in banned:
        await msg.reply_text(theme.info("User is not banned."))
        return

    banned = [x for x in banned if x != target_id]
    save_banned_users(banned)
    await msg.reply_text(theme.info(f"User {target_id} has been unbanned."))


# ================
# Trial Command
# ================

@dataclass
class TrialCheckResult:
    allowed: bool
    message: str
    trial_granted: bool


async def trial_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /trial

    Simple helper:
      - Checks if user is allowed to use a trial (using check_limits with trial_requested=True).
      - Does NOT start recording (you still use /record).
      - Does NOT increment usage (so no concurrency bug).

    Think of it as:
      "Do I still have trial slots left or not?"

    NOTE:
      To make trials actually bypass daily limits for /record, you would:
        - add a 'trial_mode' flag in user_data,
        - modify record_command to call check_limits(..., trial_requested=True)
          and add_usage(..., trial=True).
    """
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    theme = ui.get_theme(user.id)
    role = get_role(user.id)

    if role == "banned":
        await msg.reply_text(theme.error("You are banned from using this bot."))
        return

    result = check_limits(
        user_id=user.id,
        role=role,
        trial_requested=True,
        duration_seconds=0,
    )

    if not result.allowed:
        code = result.code or "unknown"
        if code == "trial_disabled":
            await msg.reply_text(theme.info("Trials are disabled by the owner."))
            return
        if code == "trials_exhausted":
            await msg.reply_text(theme.info("You have used all your trials."))
            return

        await msg.reply_text(theme.error("Trial not available."))
        return

    # We don't consume the trial here (no add_usage), just inform.
    await msg.reply_text(
        theme.info(
            "Trial is available for you ✅\n"
            "Use /record to start a session. Owner can decide how trials are enforced."
        )
    )


# ================
# Playlist Management (JSON + Mongo)
# ================

def _playlist_file(user_id: int) -> Path:
    return PLAYLIST_DIR / f"{user_id}.json"


def _json_load_playlists(user_id: int) -> Dict[str, Any]:
    return _load_json(_playlist_file(user_id), {"playlists": []})


def _json_save_playlists(user_id: int, data: Dict[str, Any]) -> None:
    _save_json(_playlist_file(user_id), data)


def _mongo_load_playlists(user_id: int) -> Dict[str, Any]:
    if not _USE_MONGO_EFFECTIVE or _mongo_playlists_coll is None:
        return _json_load_playlists(user_id)

    try:
        doc = _mongo_playlists_coll.find_one({"user_id": user_id})
        if not doc:
            return {"playlists": []}
        pls = doc.get("playlists") or []
        return {"playlists": pls}
    except Exception as e:
        logger.warning("Failed to load playlists from Mongo: %s", e)
        return _json_load_playlists(user_id)


def _mongo_save_playlists(user_id: int, data: Dict[str, Any]) -> None:
    if not _USE_MONGO_EFFECTIVE or _mongo_playlists_coll is None:
        _json_save_playlists(user_id, data)
        return

    try:
        _mongo_playlists_coll.update_one(
            {"user_id": user_id},
            {"$set": {"playlists": data.get("playlists", [])}},
            upsert=True,
        )
    except Exception as e:
        logger.warning("Failed to save playlists to Mongo: %s", e)
        _json_save_playlists(user_id, data)


def load_playlists(user_id: int) -> Dict[str, Any]:
    """
    Return a dict:
        {
          "playlists": [
            {"name": "mylist", "url": "http://..."},
            ...
          ]
        }
    """
    if _USE_MONGO_EFFECTIVE:
        return _mongo_load_playlists(user_id)
    return _json_load_playlists(user_id)


def save_playlists(user_id: int, data: Dict[str, Any]) -> None:
    if _USE_MONGO_EFFECTIVE:
        _mongo_save_playlists(user_id, data)
    else:
        _json_save_playlists(user_id, data)


async def playlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /playlist add <name> <url>
    /playlist select <name>

    - add:         store playlist under this user
    - select name: shows inline button(s) in main.py via plitem_<name>

    NOTE:
      This version treats each playlist as a single stream URL.
      Multi-channel M3U parsing can be added later.
    """
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    theme = ui.get_theme(user.id)

    if not context.args:
        await msg.reply_text(
            theme.info(
                "Usage:\n"
                "/playlist add <name> <url>\n"
                "/playlist select <name>"
            )
        )
        return

    sub = context.args[0].lower()

    if sub == "add":
        if len(context.args) < 3:
            await msg.reply_text(
                theme.error("Usage: /playlist add <name> <url>")
            )
            return
        name = context.args[1]
        url = context.args[2]

        data = load_playlists(user.id)
        pls = data.get("playlists", [])
        updated = [p for p in pls if p.get("name") != name]
        updated.append({"name": name, "url": url})
        data["playlists"] = updated
        save_playlists(user.id, data)

        await msg.reply_text(
            theme.info(f"Playlist '{name}' added for you.")
        )
        return

    if sub == "select":
        if len(context.args) < 2:
            await msg.reply_text(theme.error("Usage: /playlist select <name>"))
            return

        name = context.args[1]
        data = load_playlists(user.id)
        pls = data.get("playlists", [])
        target = None
        for p in pls:
            if p.get("name") == name:
                target = p
                break

        if not target:
            await msg.reply_text(theme.error(f"Playlist '{name}' not found."))
            return

        # The actual inline buttons are built in main.py using plitem_<id>
        # Here we only respond with info; main.py will generate buttons
        # based on load_playlists & generate_playlist_buttons.
        await msg.reply_text(
            theme.info(
                f"Playlist '{name}' is ready.\n"
                "Main bot will show buttons if implemented for plitem callbacks."
            )
        )
        return

    await msg.reply_text(
        theme.error("Unknown subcommand. Use: /playlist add|select ...")
    )


# ================
# System Metrics / Monitoring
# ================

def get_system_metrics() -> Dict[str, Any]:
    """
    Returns CPU/RAM metrics.

    Tries to use psutil if available; otherwise returns dummy values.
    """
    metrics = {
        "cpu_percent": 0.0,
        "ram_percent": 0.0,
        "ram_used_gb": 0.0,
        "ram_total_gb": 0.0,
    }

    try:
        import psutil  # type: ignore

        cpu = psutil.cpu_percent(interval=0.2)
        mem = psutil.virtual_memory()
        metrics["cpu_percent"] = float(cpu)
        metrics["ram_percent"] = float(mem.percent)
        metrics["ram_used_gb"] = mem.used / (1024**3)
        metrics["ram_total_gb"] = mem.total / (1024**3)
    except Exception:
        # psutil not installed or error – we just keep zeros
        pass

    return metrics


async def monitor_load_and_notify(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Periodic job to monitor system load and notify owner
    if CPU > 95%. Register in main.py:

        from .management import monitor_load_and_notify
        application.job_queue.run_repeating(
            monitor_load_and_notify,
            interval=60,
        )
    """
    metrics = get_system_metrics()
    cpu = metrics.get("cpu_percent", 0.0)

    if cpu > 95.0:
        text = (
            "[SYSTEM] High Load Detected.\n"
            f"CPU: {cpu:.1f}%\n"
            f"RAM: {metrics.get('ram_percent', 0.0):.1f}% "
            f"({metrics.get('ram_used_gb', 0.0):.2f}/{metrics.get('ram_total_gb', 0.0):.2f} GB)\n"
            "Throttling UI updates to prioritize recording stability."
        )
        try:
            await context.bot.send_message(chat_id=OWNER_ID, text=text)
        except Exception as e:
            logger.warning("Failed to send high-load notification to owner: %s", e)