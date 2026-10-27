"""Меню команд. Посторонним в ЛС меню не показываем вообще, участникам беты и админам — своё."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat, BotCommandScopeDefault

from .config import Settings

log = logging.getLogger(__name__)

GROUP_COMMANDS = [
    BotCommand(command="bug_report", description="Сообщить о баге"),
    BotCommand(command="feature", description="Предложить идею"),
]
MEMBER_COMMANDS = [*GROUP_COMMANDS, BotCommand(command="cancel", description="Отменить анкету")]
ADMIN_COMMANDS = [
    *MEMBER_COMMANDS,
    BotCommand(command="changelog", description="Опубликовать changelog"),
    BotCommand(command="status", description="Статус репорта: ID статус [комментарий]"),
    BotCommand(command="report", description="Открыть репорт по ID"),
    BotCommand(command="scan_topics", description="Найти топики и запостить их ID"),
    BotCommand(command="test_topics", description="Тестовый пост во все топики из .env"),
]


async def setup_commands(bot: Bot, settings: Settings) -> None:
    await bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeChat(chat_id=settings.chat_id))
    # Общие меню — пустые: посторонний в ЛС не видит ни одной команды.
    await bot.delete_my_commands(scope=BotCommandScopeAllPrivateChats())
    await bot.delete_my_commands(scope=BotCommandScopeDefault())
    for admin_id in settings.admin_ids:
        try:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
        except TelegramAPIError:  # админ мог ещё не запускать бота
            log.info("cannot set commands for admin %s", admin_id)


class PrivateMenu:
    """Меню в ЛС конкретного человека: выдаём участнику, убираем у того, кто вышел из беты."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._state: dict[int, bool] = {}  # user_id -> меню выдано

    async def sync(self, bot: Bot, user_id: int, is_member: bool) -> None:
        if self.settings.is_admin(user_id) or self._state.get(user_id) == is_member:
            return
        scope = BotCommandScopeChat(chat_id=user_id)
        try:
            if is_member:
                await bot.set_my_commands(MEMBER_COMMANDS, scope=scope)
            else:
                await bot.delete_my_commands(scope=scope)
        except TelegramAPIError as exc:
            log.info("cannot sync commands for %s: %s", user_id, exc)
            return
        self._state[user_id] = is_member
