"""Админские команды: changelog и статусы репортов."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message
from sqlalchemy.ext.asyncio import AsyncSession

from .. import keyboards, texts
from ..config import Settings
from ..db.models import ChangelogEntry
from ..services import reports
from ..services.llm import Summarizer
from ..states import ChangelogForm
from ..utils.ids import normalize_public_id
from ..utils.telegram import fmt_dt, post_link, thread_param

log = logging.getLogger(__name__)

CHANGELOG_TEXT_LIMIT = 3800


def changelog_post(version: str | None, body_html: str, settings: Settings) -> str:
    title = "📝 <b>Changelog</b>" + (f" · <code>{escape(version)}</code>" if version else "")
    return f"{title}\n\n{body_html}\n\n🕒 {fmt_dt(datetime.now(timezone.utc), settings.timezone)}"


def build_router(settings: Settings) -> Router:
    router = Router(name="admin")
    # Все команды тут только для ADMIN_IDS — остальным бот молчит.
    router.message.filter(F.from_user.id.in_(settings.admin_ids))
    router.callback_query.filter(F.from_user.id.in_(settings.admin_ids))

    # ------------------------------------------------------------ changelog (в ЛС)

    @router.message(Command("changelog"), F.chat.type == ChatType.PRIVATE)
    async def changelog_start(message: Message, state: FSMContext) -> None:
        if settings.topic_changelog_id is None:
            await message.answer(texts.TOPIC_NOT_CONFIGURED)
            return
        await state.clear()
        await state.set_state(ChangelogForm.version)
        await message.answer(texts.CHANGELOG_ASK_VERSION, reply_markup=keyboards.no_version_kb())

    async def _ask_text(message: Message, state: FSMContext, version: str | None) -> None:
        await state.update_data(version=version)
        await state.set_state(ChangelogForm.text)
        await message.answer(texts.CHANGELOG_ASK_TEXT)

    @router.message(ChangelogForm.version, F.text, ~F.text.startswith("/"))
    async def changelog_version(message: Message, state: FSMContext) -> None:
        await _ask_text(message, state, message.text.strip()[:64])  # type: ignore[union-attr]

    @router.callback_query(ChangelogForm.version, keyboards.FlowCb.filter(F.action == "no_version"))
    async def changelog_no_version(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(reply_markup=None)
            await _ask_text(callback.message, state, None)

    @router.message(ChangelogForm.text, F.text, ~F.text.startswith("/"))
    async def changelog_text(message: Message, state: FSMContext) -> None:
        body = message.html_text
        if len(body) > CHANGELOG_TEXT_LIMIT:
            await message.answer(texts.TOO_LONG.format(n=len(body), max=CHANGELOG_TEXT_LIMIT))
            return
        data = await state.update_data(body=body)
        await state.set_state(ChangelogForm.confirm)
        await message.answer(texts.PREVIEW_HEADER)
        await message.answer(
            changelog_post(data.get("version"), body, settings),
            reply_markup=keyboards.confirm_kb(),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    @router.callback_query(ChangelogForm.confirm, keyboards.FlowCb.filter(F.action == "submit"))
    async def changelog_submit(callback: CallbackQuery, state: FSMContext, bot: Bot, session: AsyncSession) -> None:
        data = await state.get_data()
        await state.clear()
        if not data.get("body"):
            await callback.answer()
            return
        try:
            msg = await bot.send_message(
                settings.chat_id,
                changelog_post(data.get("version"), data["body"], settings),
                message_thread_id=thread_param(settings.topic_changelog_id),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramAPIError as exc:
            await state.set_state(ChangelogForm.confirm)
            await state.set_data(data)
            await callback.answer(f"Не получилось опубликовать: {exc.message}", show_alert=True)
            return
        session.add(
            ChangelogEntry(
                version=data.get("version"),
                text_html=data["body"],
                author_id=callback.from_user.id,
                post_message_id=msg.message_id,
            )
        )
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(reply_markup=None)
            link = post_link(msg.chat.id, msg.message_id, settings.topic_changelog_id) or ""
            await callback.message.answer(texts.SENT_CHANGELOG.format(link=link))

    @router.callback_query(ChangelogForm.confirm, keyboards.FlowCb.filter(F.action == "restart"))
    async def changelog_restart(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.clear()
        await state.set_state(ChangelogForm.version)
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(texts.CHANGELOG_ASK_VERSION, reply_markup=keyboards.no_version_kb())

    # ------------------------------------------------------------ статусы (в ЛС или в чате)

    @router.message(Command("status"))
    async def set_status(
        message: Message,
        command: CommandObject,
        bot: Bot,
        session: AsyncSession,
        summarizer: Summarizer,
    ) -> None:
        parts = (command.args or "").split(maxsplit=2)
        if len(parts) < 2 or parts[1] not in texts.STATUS_LABELS:
            await message.answer(texts.STATUS_USAGE)
            return
        rid = normalize_public_id(parts[0])
        report = await reports.get_report(session, rid) if rid else None
        if report is None:
            await message.answer(texts.REPORT_NOT_FOUND)
            return
        report.status = parts[1]
        report.status_comment = parts[2] if len(parts) > 2 else None
        report.updated_at = datetime.now(timezone.utc)
        await session.flush()
        await reports.refresh_post(bot, report, settings, summarizer)
        label = reports.status_label(report.status)
        await message.answer(texts.STATUS_CHANGED.format(rid=report.public_id, status=label))
        comment = f"\n{escape(report.status_comment)}" if report.status_comment else ""
        try:
            await bot.send_message(
                report.user_id, texts.STATUS_CHANGED_AUTHOR.format(rid=report.public_id, status=label, comment=comment)
            )
        except TelegramAPIError:
            pass

    return router
