"""Who moderation leaves alone: members with ADMIN/MODER tags (chat member tags — Bot API 9.x)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from .. import texts
from ..config import CONFIG, Settings

log = logging.getLogger(__name__)


@dataclass(slots=True)
class MemberInfo:
    status: str
    tag: str | None
    custom_title: str | None


def tag_matches(tag: str | None, exempt: set[str]) -> bool:
    return bool(tag) and tag.strip().casefold() in exempt  # type: ignore[union-attr]


class MemberTagResolver:
    """get_chat_member with a short cache, to avoid hitting the API on every message."""

    def __init__(self, ttl: float | None = None, absent_ttl: float | None = None):
        self.ttl = ttl if ttl is not None else CONFIG["moderation"]["member_cache_ttl"]
        self.absent_ttl = absent_ttl if absent_ttl is not None else CONFIG["moderation"]["member_absent_cache_ttl"]
        self._cache: dict[tuple[int, int], tuple[float, MemberInfo]] = {}

    async def get(self, bot: Bot, chat_id: int, user_id: int) -> MemberInfo | None:
        key = (chat_id, user_id)
        cached = self._cache.get(key)
        if cached:
            # Cache non-members briefly: they may have just joined.
            ttl = self.ttl if cached[1].status not in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED) else self.absent_ttl
            if time.monotonic() - cached[0] < ttl:
                return cached[1]
        try:
            member = await bot.get_chat_member(chat_id, user_id)
        except TelegramAPIError as exc:
            log.warning(texts.LOG["member_tags"]["get_failed"], chat_id, user_id, exc)
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
        # Anonymous admin, or a post on behalf of the chat itself / a linked channel.
        if message.sender_chat is not None:
            return message.sender_chat.id == message.chat.id or bool(message.is_automatic_forward)
        user = message.from_user
        if user is None:
            return True
        if settings.is_admin(user.id):
            return True
        # Telegram puts the tag (or an admin's custom title) right on the message — the primary path.
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
