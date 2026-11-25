"""Admin commands: changelog and report statuses."""

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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import keyboards, texts
from ..config import CONFIG, Settings
from ..db.models import ChangelogEntry, Topic
from ..services import reports
from ..services.llm import Summarizer
from ..services.notify import announce
from ..services.prefs import Prefs
from ..services.topic_scan import report_text, scan_topics, test_configured_topics
from ..states import ChangelogForm
from ..utils.ids import normalize_public_id
from ..utils.telegram import fmt_dt, post_link, spawn, thread_param

log = logging.getLogger(__name__)

CHANGELOG_TEXT_LIMIT = CONFIG["reports"]["changelog_text_limit"]


def changelog_post(version: str | None, body_html: str, settings: Settings) -> str:
    title = texts.CHANGELOG_TITLE_VERSION.format(version=escape(version)) if version else texts.CHANGELOG_TITLE
    return texts.CHANGELOG_POST.format(
        title=title, body=body_html, dt=fmt_dt(datetime.now(timezone.utc), settings.timezone)
    )


def build_router(settings: Settings) -> Router:
    router = Router(name="admin")
    # Everything here is ADMIN_IDS only — the bot stays silent for everyone else.
    router.message.filter(F.from_user.id.in_(settings.admin_ids))
    router.callback_query.filter(F.from_user.id.in_(settings.admin_ids))

    # ------------------------------------------------------------ changelog (in private chat)

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
            await callback.answer(texts.CHANGELOG_PUBLISH_FAILED.format(error=exc.message), show_alert=True)
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

    # ------------------------------------------------------------ topic discovery

    @router.message(Command("scan_topics"), F.chat.type == ChatType.PRIVATE)
    async def scan_topics_cmd(message: Message, command: CommandObject, bot: Bot, session: AsyncSession) -> None:
        max_id = int(command.args) if (command.args or "").isdigit() else CONFIG["topic_scan"]["max_thread_id"]
        known = set(
            (await session.execute(select(Topic.thread_id).where(Topic.chat_id == settings.chat_id))).scalars()
        )
        await message.answer(texts.SCAN_STARTED.format(max=max_id))

        async def run() -> None:
            found = await scan_topics(bot, settings.chat_id, max_thread_id=max_id, known=known)
            await message.answer(report_text(found, max_id))

        spawn(run())

    @router.message(Command("test_topics"), F.chat.type == ChatType.PRIVATE)
    async def test_topics_cmd(message: Message, bot: Bot) -> None:
        lines = await test_configured_topics(bot, settings)
        await message.answer(texts.TEST_HEADER + "\n" + "\n".join(lines))

    # ------------------------------------------------------------ statuses (in private chat or the group)

    @router.message(Command("status"))
    async def set_status(
        message: Message,
        command: CommandObject,
        bot: Bot,
        session: AsyncSession,
        summarizer: Summarizer,
        prefs: Prefs,
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
        await reports.apply_status(bot, session, report, parts[1], parts[2] if len(parts) > 2 else None, settings, summarizer)
        label = reports.status_label(report.status)
        await message.answer(texts.STATUS_CHANGED.format(rid=report.public_id, status=label))
        await announce(
            bot, settings, prefs, "status_change",
            texts.STATUS_CHANGE_ANNOUNCE.format(rid=report.public_id, status=label),
        )

    return router
