"""Команды в бета-чате: /bug_report, /feature, /topic_id и кнопка «Подробнее»."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from .. import keyboards, texts
from ..config import Settings
from ..services import reports
from ..services.member_tags import MemberTagResolver
from ..utils.ids import normalize_public_id
from ..utils.telegram import delete_later, thread_param, topic_of, user_html_from
from .forms import check_can_report, start_flow

log = logging.getLogger(__name__)


def build_router(settings: Settings) -> Router:
    router = Router(name="group")
    router.message.filter(F.chat.id == settings.chat_id)

    async def _reply(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        sent = await message.bot.send_message(  # type: ignore[union-attr]
            message.chat.id, text, message_thread_id=thread_param(topic_of(message)), reply_markup=markup
        )
        delete_later(sent, settings.group_reply_ttl)

    @router.message(Command("bug_report", "feature"))
    async def report_command(
        message: Message,
        command: CommandObject,
        state: FSMContext,
        bot: Bot,
        tags: MemberTagResolver,
        bot_username: str,
    ) -> None:
        user = message.from_user
        if user is None:
            return
        kind = reports.KIND_BUG if command.command == "bug_report" else reports.KIND_FEATURE
        if settings.delete_group_commands:
            try:
                await message.delete()
            except TelegramAPIError:
                pass
        if reports.topic_for(kind, settings) is None:
            await _reply(message, texts.TOPIC_NOT_CONFIGURED)
            return
        denied = await check_can_report(bot, user, settings, tags)
        if denied:
            await _reply(message, denied)
            return

        # FSM живёт в ЛС, поэтому состояние ставим на ключ приватного чата, а не группы.
        pm_state = FSMContext(
            storage=state.storage, key=StorageKey(bot_id=bot.id, chat_id=user.id, user_id=user.id)
        )
        await pm_state.clear()
        mention = user_html_from(user)
        try:
            await start_flow(bot, pm_state, user.id, kind)
        except TelegramForbiddenError:
            payload = "bug" if kind == reports.KIND_BUG else "feature"
            await _reply(
                message,
                texts.GROUP_OPEN_PM.format(user=mention),
                keyboards.url_kb(texts.GROUP_OPEN_PM_BUTTON, f"https://t.me/{bot_username}?start={payload}"),
            )
            return
        await _reply(
            message,
            texts.GROUP_GO_TO_PM.format(user=mention),
            keyboards.url_kb(texts.GROUP_OPEN_PM_BUTTON, f"https://t.me/{bot_username}"),
        )

    @router.chat_member(F.chat.id == settings.chat_id)
    async def member_changed(event: ChatMemberUpdated, tags: MemberTagResolver) -> None:
        # Вступил/вышел/сменил тег — сбрасываем кеш, чтобы доступ в ЛС и исключения модерации обновились сразу.
        tags.invalidate(event.chat.id, event.new_chat_member.user.id)

    @router.message(Command("topic_id"))
    async def topic_id(message: Message) -> None:
        if not message.from_user or not settings.is_admin(message.from_user.id):
            return
        topic = topic_of(message)
        await message.reply(
            f"chat_id: <code>{message.chat.id}</code>\n"
            f"message_thread_id: <code>{topic}</code>" + (" (General)" if topic == 0 else "")
        )

    @router.callback_query(keyboards.DetailsCb.filter())
    async def details(
        callback: CallbackQuery,
        callback_data: keyboards.DetailsCb,
        session: AsyncSession,
        bot_username: str,
    ) -> None:
        rid = normalize_public_id(callback_data.rid)
        report = await reports.get_report(session, rid) if rid else None
        if report is None:
            await callback.answer(texts.REPORT_NOT_FOUND, show_alert=True)
            return
        if not reports.can_view(report, callback.from_user.id, settings):
            await callback.answer(texts.DETAILS_DENIED, show_alert=True)
            return
        # answerCallbackQuery умеет открыть t.me/<bot>?start=... — это и есть «перебросить в личку».
        await callback.answer(url=f"https://t.me/{bot_username}?start=r_{report.public_id}")

    return router
