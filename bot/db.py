# bot/db.py
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Dict, Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, IndexModel

from .config import (
    USE_MONGO,
    MONGO_URI,
    MONGO_DB_NAME,
    COL_USERS,
    COL_PLAYLISTS,
    COL_SETTINGS,
    COL_USAGE,
    COL_SCHEDULES,
)

class DBError(RuntimeError):
    pass

@dataclass
class DB:
    client: AsyncIOMotorClient
    db: AsyncIOMotorDatabase

    @classmethod
    async def connect(cls) -> "DB":
        if not USE_MONGO:
            raise DBError("USE_MONGO is disabled, but this bot requires MongoDB for full features.")
        if not MONGO_URI:
            raise DBError("MONGO_URI is not set. Please set MONGO_URI in environment.")
        client = AsyncIOMotorClient(MONGO_URI)
        db = client[MONGO_DB_NAME]
        inst = cls(client=client, db=db)
        await inst.ensure_indexes()
        return inst

    async def ensure_indexes(self) -> None:
        users = self.db[COL_USERS]
        playlists = self.db[COL_PLAYLISTS]
        usage = self.db[COL_USAGE]
        schedules = self.db[COL_SCHEDULES]
        settings = self.db[COL_SETTINGS]

        await users.create_indexes([
            IndexModel([("user_id", ASCENDING)], unique=True),
        ])
        await playlists.create_indexes([
            IndexModel([("user_id", ASCENDING)], unique=True),
        ])
        await usage.create_indexes([
            IndexModel([("user_id", ASCENDING), ("day", ASCENDING)], unique=True),
        ])
        await schedules.create_indexes([
            IndexModel([("schedule_id", ASCENDING)], unique=True),
            IndexModel([("run_at", ASCENDING)]),
            IndexModel([("user_id", ASCENDING)]),
        ])
        await settings.create_indexes([
            IndexModel([("key", ASCENDING)], unique=True),
        ])

    async def close(self) -> None:
        self.client.close()

    # --------------------------
    # Settings
    # --------------------------
    async def get_setting(self, key: str, default: Any = None) -> Any:
        doc = await self.db[COL_SETTINGS].find_one({"key": key})
        return doc["value"] if doc and "value" in doc else default

    async def set_setting(self, key: str, value: Any) -> None:
        await self.db[COL_SETTINGS].update_one(
            {"key": key},
            {"$set": {"value": value, "updated_at": _dt.datetime.utcnow()}},
            upsert=True,
        )

    # --------------------------
    # Users
    # --------------------------
    async def ensure_user(self, user_id: int) -> Dict[str, Any]:
        now = _dt.datetime.utcnow()
        doc = await self.db[COL_USERS].find_one({"user_id": user_id})
        if doc:
            return doc
        doc = {
            "user_id": user_id,
            "theme": None,
            "premium_until": None,
            "trial_credits": 0,
            "created_at": now,
            "updated_at": now,
        }
        await self.db[COL_USERS].insert_one(doc)
        return doc

    async def update_user(self, user_id: int, patch: Dict[str, Any]) -> None:
        patch["updated_at"] = _dt.datetime.utcnow()
        await self.db[COL_USERS].update_one({"user_id": user_id}, {"$set": patch}, upsert=True)

    async def get_user(self, user_id: int) -> Dict[str, Any]:
        doc = await self.db[COL_USERS].find_one({"user_id": user_id})
        return doc or await self.ensure_user(user_id)

    # --------------------------
    # Usage
    # --------------------------
    async def get_usage(self, user_id: int, day: str) -> Dict[str, Any]:
        doc = await self.db[COL_USAGE].find_one({"user_id": user_id, "day": day})
        if doc:
            return doc
        doc = {"user_id": user_id, "day": day, "used_seconds": 0}
        await self.db[COL_USAGE].insert_one(doc)
        return doc

    async def add_usage(self, user_id: int, day: str, seconds: int) -> None:
        await self.db[COL_USAGE].update_one(
            {"user_id": user_id, "day": day},
            {"$inc": {"used_seconds": int(seconds)}, "$set": {"updated_at": _dt.datetime.utcnow()}},
            upsert=True,
        )

    async def set_usage(self, user_id: int, day: str, used_seconds: int) -> None:
        await self.db[COL_USAGE].update_one(
            {"user_id": user_id, "day": day},
            {"$set": {"used_seconds": int(used_seconds), "updated_at": _dt.datetime.utcnow()}},
            upsert=True,
        )

    # --------------------------
    # Playlists
    # --------------------------
    async def get_playlist(self, user_id: int) -> Optional[Dict[str, Any]]:
        return await self.db[COL_PLAYLISTS].find_one({"user_id": user_id})

    async def set_playlist(self, user_id: int, playlist_doc: Dict[str, Any]) -> None:
        playlist_doc["updated_at"] = _dt.datetime.utcnow()
        await self.db[COL_PLAYLISTS].update_one(
            {"user_id": user_id},
            {"$set": playlist_doc},
            upsert=True,
        )

    # --------------------------
    # Schedules
    # --------------------------
    async def create_schedule(self, schedule_doc: Dict[str, Any]) -> None:
        schedule_doc["created_at"] = _dt.datetime.utcnow()
        schedule_doc.setdefault("status", "scheduled")
        await self.db[COL_SCHEDULES].insert_one(schedule_doc)

    async def update_schedule(self, schedule_id: str, patch: Dict[str, Any]) -> None:
        patch["updated_at"] = _dt.datetime.utcnow()
        await self.db[COL_SCHEDULES].update_one({"schedule_id": schedule_id}, {"$set": patch})

    async def get_schedules_for_user(self, user_id: int, limit: int = 25):
        cursor = self.db[COL_SCHEDULES].find({"user_id": user_id}).sort("run_at", ASCENDING).limit(limit)
        return [doc async for doc in cursor]
