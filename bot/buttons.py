# bot/buttons.py
from __future__ import annotations

from typing import List, Dict, Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# Callback format:
#   quality: "q|<task_id>|<variant_id>"
#   audio:   "a|<task_id>|<audio_id>" or "a|<task_id>|ALL"
#   cancel:  "c|<task_id>"
#   proxy:   "px|rm"

def quality_keyboard(task_id: str, variants: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for v in variants:
        label = v.get("label") or v.get("id")
        rows.append([InlineKeyboardButton(f"📽️ {label}", callback_data=f"q|{task_id}|{v['id']}")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"c|{task_id}")])
    return InlineKeyboardMarkup(rows)

def audio_keyboard(task_id: str, audios: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    # "All" button (select all audio tracks)
    rows.append([InlineKeyboardButton("🎶 All", callback_data=f"a|{task_id}|ALL")])
    for a in audios:
        label = a.get("label") or a.get("name") or a.get("id")
        emoji = "🎶"
        rows.append([InlineKeyboardButton(f"{emoji} {label}", callback_data=f"a|{task_id}|{a['id']}")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"c|{task_id}")])
    return InlineKeyboardMarkup(rows)

def proxy_remove_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Remove Proxy", callback_data="px|rm")],
    ])
