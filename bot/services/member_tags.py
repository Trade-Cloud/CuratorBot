"""Кого модерация не трогает: участники с тегами ADMIN/MODER (теги участников чата — Bot API 9.x)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from ..config import Settings

log = logging.getLogger(__name__)


@dataclass(slots=True)
class MemberInfo:
    status: str
    tag: str | None
    custom_title: str | None


def tag_matches(tag: str | None, exempt: set[str]) -> bool:
    return bool(tag) and tag.strip().casefold() in exempt  # type: ignore[union-attr]


class MemberTagResolver:
    """get_chat_member с коротким кешем — чтобы не дёргать API на каждое сообщение."""

    def __init__(self, ttl: float = 300.0):
        self.ttl = ttl
        self._cache: dict[tuple[int, int], tuple[float, MemberInfo]] = {}

    async def get(self, bot: Bot, chat_id: int, user_id: int) -> MemberInfo | None:
        key = (chat_id, user_id)
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < self.ttl:
            return cached[1]
        try:
            member = await bot.get_chat_member(chat_id, user_id)
        except TelegramAPIError as exc:
            log.warning("get_chat_member(%s, %s) failed: %s", chat_id, user_id, exc)
            return None
        info = MemberInfo(
            status=str(member.status),
            tag=getattr(member, "tag", None),
            custom_title=getattr(member, "custom_title", None),
        )
        self._cache[key] = (time.monotonic(), info)
        return info

    def invalidate(self, chat_id: int, user_id: int) -> None:
        self._cache.pop((chat_id, user_id), None)

    async def is_exempt(self, message: Message, settings: Settings) -> bool:
        # Анонимный админ или пост от имени самого чата/привязанного канала.
        if message.sender_chat is not None:
            return message.sender_chat.id == message.chat.id or bool(message.is_automatic_forward)
        user = message.from_user
        if user is None:
            return True
        if settings.is_admin(user.id):
            return True
        # Telegram кладёт тег (или кастомный титул админа) прямо в сообщение — это основной путь.
        if tag_matches(message.sender_tag, settings.exempt_tags):
            return True
        if user.is_bot and message.bot and user.id == message.bot.id:
            return True
        info = await self.get(message.bot, message.chat.id, user.id) if message.bot else None
        if info is None:
            return False
        if tag_matches(info.tag, settings.exempt_tags) or tag_matches(info.custom_title, settings.exempt_tags):
            return True
        return settings.exempt_chat_admins and info.status in (
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.ADMINISTRATOR,
        )

    async def is_member(self, bot: Bot, chat_id: int, user_id: int) -> bool:
        info = await self.get(bot, chat_id, user_id)
        if info is None:
            return False
        return info.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)
