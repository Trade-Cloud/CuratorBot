"""Chat member actions: mute/unmute, ban/unban, kick. Used from both the bot and the web."""

from __future__ import annotations

from datetime import timedelta

from aiogram import Bot
from aiogram.types import ChatPermissions

from .. import texts
from ..db.models import utcnow

ACTIONS = {"mute", "unmute", "ban", "unban", "kick"}
# Ask for confirmation on dangerous actions.
DESTRUCTIVE = {"ban", "kick"}

ACTION_LABELS = texts.ACTION_LABELS

_MUTED = ChatPermissions(can_send_messages=False)
_UNMUTED = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
)


async def apply_action(
    bot: Bot, chat_id: int, user_id: int, action: str, minutes: int | None = None
) -> str:
    """Perform the action and return a short result label (for the admin report). Raises TelegramAPIError."""
    if action == "mute":
        until = utcnow() + timedelta(minutes=minutes) if minutes else None
        await bot.restrict_chat_member(chat_id, user_id, permissions=_MUTED, until_date=until)
    elif action == "unmute":
        await bot.restrict_chat_member(chat_id, user_id, permissions=_UNMUTED)
    elif action == "ban":
        await bot.ban_chat_member(chat_id, user_id)
    elif action == "unban":
        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
    elif action == "kick":
        # Kick = ban + unban, so the person can rejoin via an invite.
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
    else:
        raise ValueError(f"unknown action: {action}")
    return ACTION_LABELS[action]
