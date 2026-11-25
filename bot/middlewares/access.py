from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject

from .. import keyboards, texts
from ..commands import PrivateMenu
from ..config import Settings
from ..services.member_tags import MemberTagResolver


class PrivateAccessMiddleware(BaseMiddleware):
    """The bot's private chat is for beta chat members and admins only.
    Everyone else sees nothing but a link to the main bot, where beta access is requested."""

    def __init__(self, settings: Settings, tags: MemberTagResolver, menu: PrivateMenu):
        self.settings = settings
        self.tags = tags
        self.menu = menu
        self._last_notice: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            chat = event.chat
        elif isinstance(event, CallbackQuery) and isinstance(event.message, Message):
            chat = event.message.chat
        else:
            return await handler(event, data)
        user = event.from_user
        if chat.type != ChatType.PRIVATE or user is None:
            return await handler(event, data)

        bot: Bot = data["bot"]
        allowed = self.settings.is_admin(user.id) or await self.tags.is_member(bot, self.settings.chat_id, user.id)
        await self.menu.sync(bot, user.id, allowed)
        if allowed:
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            await event.answer(texts.NO_ACCESS_SHORT, show_alert=True)
            return None
        # Don't spam the same reply on every message.
        now = time.monotonic()
        if now - self._last_notice.get(user.id, 0) > 30:
            self._last_notice[user.id] = now
            bot_name = self.settings.access_bot_username.lstrip("@")
            await event.answer(
                texts.NO_ACCESS.format(bot=bot_name),
                reply_markup=keyboards.url_kb(texts.NO_ACCESS_BUTTON, f"https://t.me/{bot_name}"),
            )
        return None
