"""Command menus. Outsiders see no menu in private chat; beta members and admins get their own."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat, BotCommandScopeDefault

from . import texts
from .config import Settings

log = logging.getLogger(__name__)

GROUP_COMMANDS = [
    BotCommand(command="bug_report", description=texts.COMMANDS["bug_report"]),
    BotCommand(command="feature", description=texts.COMMANDS["feature"]),
]
MEMBER_COMMANDS = [*GROUP_COMMANDS, BotCommand(command="cancel", description=texts.COMMANDS["cancel"])]
ADMIN_COMMANDS = [
    *MEMBER_COMMANDS,
    BotCommand(command="admin", description=texts.COMMANDS["admin"]),
    BotCommand(command="changelog", description=texts.COMMANDS["changelog"]),
    BotCommand(command="status", description=texts.COMMANDS["status"]),
    BotCommand(command="report", description=texts.COMMANDS["report"]),
    BotCommand(command="scan_topics", description=texts.COMMANDS["scan_topics"]),
    BotCommand(command="test_topics", description=texts.COMMANDS["test_topics"]),
]


async def setup_commands(bot: Bot, settings: Settings) -> None:
    # The bot may not be in the chat yet (e.g. a fresh token) — then chat commands cannot be set,
    # but that is no reason to fail the whole startup.
    try:
        await bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeChat(chat_id=settings.chat_id))
    except TelegramAPIError as exc:
        log.warning(texts.LOG["commands"]["set_chat_failed"], settings.chat_id, exc)
    # Shared menus stay empty: an outsider in private chat sees no commands at all.
    try:
        await bot.delete_my_commands(scope=BotCommandScopeAllPrivateChats())
        await bot.delete_my_commands(scope=BotCommandScopeDefault())
    except TelegramAPIError as exc:
        log.warning(texts.LOG["commands"]["clear_shared_failed"], exc)
    for admin_id in settings.admin_ids:
        try:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
        except TelegramAPIError:  # an admin may not have started the bot yet
            log.info(texts.LOG["commands"]["set_admin_failed"], admin_id)


class PrivateMenu:
    """Per-person private-chat menu: granted to members, removed from anyone who leaves the beta."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._state: dict[int, bool] = {}  # user_id -> menu granted

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
            log.info(texts.LOG["commands"]["sync_failed"], user_id, exc)
            return
        self._state[user_id] = is_member
