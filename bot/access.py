# bot/access.py
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from .config import OWNER_ID, GROUP_ID
from .messages import Msg
from .ui import get_theme_for_user

async def enforce_access_or_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Access rules:
    - OWNER has NO limitations: can use anywhere.
    - Everyone else: ONLY allowed inside GROUP_ID.
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False

    if user.id == OWNER_ID:
        return True

    if chat.id != GROUP_ID:
        theme = await get_theme_for_user(context, user.id)
        await update.effective_message.reply_text(
            Msg.get(theme, "access.group_only", group_id=str(GROUP_ID))
        )
        return False
    return True
