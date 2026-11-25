"""Commands in the beta chat: /bug_report, /feature, /topic_id and the "Details" button."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
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
from ..services.notify import announce
from ..services.prefs import Prefs
from ..utils.ids import normalize_public_id
from ..utils.telegram import delete_later, thread_param, topic_of, user_html_from
from .forms import check_can_report, start_flow

log = logging.getLogger(__name__)

_PRESENT = {ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
_ABSENT = {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}


def build_router(settings: Settings) -> Router:
    router = Router(name="group")
    router.message.filter(F.chat.id == settings.chat_id)

    async def _reply(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        sent = await message.bot.send_message(  # type: ignore[union-attr]
            message.chat.id, text, message_thread_id=thread_param(topic_of(message)), reply_markup=markup
        )
        delete_later(sent, settings.group_reply_ttl)

    async def _delete_command(message: Message) -> None:
        """Delete the command message in the group. If the bot lacks the right, log an explicit WARNING
        so the reason is visible (most often the bot was not granted 'Delete messages')."""
        if not settings.delete_group_commands:
            return
        try:
            await message.delete()
        except TelegramAPIError as exc:
            log.warning(texts.LOG["group"]["delete_command_failed"], exc)

    @router.message(Command("bug_report", "feature"))
    async def report_command(
        message: Message,
        command: CommandObject,
        state: FSMContext,
        bot: Bot,
        tags: MemberTagResolver,
        prefs: Prefs,
        session: AsyncSession,
        bot_username: str,
    ) -> None:
        user = message.from_user
        if user is None:
            return
        kind = reports.KIND_BUG if command.command == "bug_report" else reports.KIND_FEATURE
        await _delete_command(message)
        if reports.topic_for(kind, settings) is None:
            await _reply(message, texts.TOPIC_NOT_CONFIGURED)
            return
        denied = await check_can_report(bot, user, settings, tags)
        if denied:
            await _reply(message, denied)
            return

        # The FSM lives in the private chat, so the state is keyed to the private chat, not the group.
        pm_state = FSMContext(
            storage=state.storage, key=StorageKey(bot_id=bot.id, chat_id=user.id, user_id=user.id)
        )
        await pm_state.clear()
        mention = user_html_from(user)
        try:
            await start_flow(bot, pm_state, kind, user, session, settings, prefs)
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
    async def member_changed(
        event: ChatMemberUpdated, tags: MemberTagResolver, bot: Bot, prefs: Prefs
    ) -> None:
        # Joined/left/changed tag — drop the cache so private-chat access and moderation exemptions update at once.
        user = event.new_chat_member.user
        tags.invalidate(event.chat.id, user.id)
        joined = event.old_chat_member.status in _ABSENT and event.new_chat_member.status in _PRESENT
        if joined and not user.is_bot:
            await announce(
                bot, settings, prefs, "member_join", texts.MEMBER_JOIN.format(user=user_html_from(user))
            )

    @router.message(Command("topic_id"))
    async def topic_id(message: Message) -> None:
        if not message.from_user or not settings.is_admin(message.from_user.id):
            return
        topic = topic_of(message)
        await message.reply(
            texts.TOPIC_ID_INFO.format(chat=message.chat.id, topic=topic)
            + (texts.TOPIC_ID_GENERAL_SUFFIX if topic == 0 else "")
        )
        await _delete_command(message)

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
        # answerCallbackQuery can open t.me/<bot>?start=... — this is the "send to private chat" jump.
        await callback.answer(url=f"https://t.me/{bot_username}?start=r_{report.public_id}")

    return router
