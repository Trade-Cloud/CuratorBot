"""Private-chat forms: bug report and feature request."""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, User
from sqlalchemy.ext.asyncio import AsyncSession

from .. import keyboards, texts
from ..config import Settings
from ..services import drafts, reports
from ..services.llm import LLMSummarizer, Summarizer
from ..services.media import FileRef, MediaDownloader, extract_files
from ..services.member_tags import MemberTagResolver
from ..services.notify import announce
from ..services.prefs import Prefs
from ..states import BugForm, FeatureForm
from ..web.events import bus
from ..utils.telegram import KeyedLock, split_html_lines, user_html_from

log = logging.getLogger(__name__)

router = Router(name="forms")
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

_locks = KeyedLock()

# Step -> (field, next step, question for the next step)
_BUG_TEXT_STEPS: dict[State, tuple[str, State, str]] = {
    BugForm.description: ("description", BugForm.steps, texts.BUG_ASK_STEPS),
    BugForm.steps: ("steps", BugForm.actual, texts.BUG_ASK_ACTUAL),
    BugForm.actual: ("actual", BugForm.expected, texts.BUG_ASK_EXPECTED),
    BugForm.expected: ("expected", BugForm.environment, texts.BUG_ASK_ENVIRONMENT),
}
_FEATURE_TEXT_STEPS: dict[State, tuple[str, State, str]] = {
    FeatureForm.idea: ("idea", FeatureForm.problem, texts.FEATURE_ASK_PROBLEM),
    FeatureForm.problem: ("problem", FeatureForm.implementation, texts.FEATURE_ASK_IMPLEMENTATION),
}
_TEXT_STEPS_BY_NAME = {s.state: v for s, v in {**_BUG_TEXT_STEPS, **_FEATURE_TEXT_STEPS}.items()}

# State -> field filled on it (needed for quick buttons and skipping).
_FIELD_BY_STATE: dict[State, str] = {
    BugForm.description: "description",
    BugForm.steps: "steps",
    BugForm.actual: "actual",
    BugForm.expected: "expected",
    BugForm.environment: "environment",
    FeatureForm.idea: "idea",
    FeatureForm.problem: "problem",
    FeatureForm.implementation: "implementation",
}
_FIELD_BY_STATE_STR = {s.state: f for s, f in _FIELD_BY_STATE.items()}


async def _step_markup(prefs: Prefs, field: str) -> InlineKeyboardMarkup | None:
    return keyboards.step_kb(await prefs.list_buttons(field), field, skip=True)


# ---------------------------------------------------------------- form start (also used from the group)


async def start_flow(
    bot: Bot, state: FSMContext, kind: str, user: User, session: AsyncSession, settings: Settings, prefs: Prefs
) -> None:
    """Send the first question in private chat, set the state and create a draft. Raises TelegramForbiddenError
    if the user has not started the bot yet — then the caller offers a deep link."""
    started = datetime.now(timezone.utc)
    if kind == reports.KIND_BUG:
        await bot.send_message(user.id, texts.BUG_INTRO)
        first, question = BugForm.description, texts.BUG_ASK_DESCRIPTION
    else:
        await bot.send_message(user.id, texts.FEATURE_INTRO)
        first, question = FeatureForm.idea, texts.FEATURE_ASK_IDEA
    await bot.send_message(user.id, question, reply_markup=await _step_markup(prefs, _FIELD_BY_STATE[first]))
    await state.set_state(first)
    await state.set_data({"kind": kind, "fields": {}, "files": [], "started_at": started.isoformat()})
    is_new = await drafts.start(session, user, kind, started)
    if is_new:
        label = texts.DRAFT_LABEL_BUG if kind == reports.KIND_BUG else texts.DRAFT_LABEL_FEATURE
        await announce(
            bot, settings, prefs, "draft_new", texts.DRAFT_STARTED.format(label=label, user=user_html_from(user))
        )


async def check_can_report(bot: Bot, user: User, settings: Settings, tags: MemberTagResolver) -> str | None:
    """Refusal text, or None if allowed."""
    if settings.require_membership and not settings.is_admin(user.id):
        if not await tags.is_member(bot, settings.chat_id, user.id):
            return texts.NOT_A_MEMBER
    return None


async def begin_from_private(
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    tags: MemberTagResolver,
    prefs: Prefs,
    session: AsyncSession,
    kind: str,
) -> None:
    assert message.from_user
    if reports.topic_for(kind, settings) is None:
        await message.answer(texts.TOPIC_NOT_CONFIGURED)
        return
    denied = await check_can_report(bot, message.from_user, settings, tags)
    if denied:
        await message.answer(denied)
        return
    await state.clear()
    await start_flow(bot, state, kind, message.from_user, session, settings, prefs)


@router.message(Command("bug_report", "bug"))
async def cmd_bug(
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    tags: MemberTagResolver,
    prefs: Prefs,
    session: AsyncSession,
) -> None:
    await begin_from_private(message, state, bot, settings, tags, prefs, session, reports.KIND_BUG)


@router.message(Command("feature"))
async def cmd_feature(
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    tags: MemberTagResolver,
    prefs: Prefs,
    session: AsyncSession,
) -> None:
    await begin_from_private(message, state, bot, settings, tags, prefs, session, reports.KIND_FEATURE)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if await state.get_state() is None:
        await message.answer(texts.NOTHING_TO_CANCEL)
        return
    assert message.from_user
    await state.clear()
    await drafts.mark_cancelled(session, message.from_user.id)
    await message.answer(texts.CANCELLED)


# ---------------------------------------------------------------- text steps


async def _read_text(message: Message, field: str) -> str | None:
    if not message.text or message.text.startswith("/"):
        await message.answer(texts.NEED_TEXT)
        return None
    value = message.text.strip()
    limit = reports.FIELD_LIMITS[field]
    if len(value) > limit:
        await message.answer(texts.TOO_LONG.format(n=len(value), max=limit))
        return None
    return value


async def _save_field(state: FSMContext, field: str, value: str) -> dict:
    data = await state.get_data()
    data.setdefault("fields", {})[field] = value
    await state.set_data(data)
    return data


async def _persist_draft(session: AsyncSession, user_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    await drafts.update(
        session,
        user_id,
        fields=data.get("fields", {}),
        files_count=len(data.get("files", [])),
        step=await state.get_state(),
    )


async def _ask_attachments(message: Message, settings: Settings) -> None:
    optional = not settings.bug_attachments_required
    text = texts.BUG_ASK_ATTACHMENTS_OPTIONAL if optional else texts.BUG_ASK_ATTACHMENTS
    await message.answer(text, reply_markup=keyboards.done_kb(skip=optional))


async def _advance(
    message: Message,
    state: FSMContext,
    bot: Bot,
    prefs: Prefs,
    session: AsyncSession,
    settings: Settings,
    user_id: int,
    value: str | None,
) -> None:
    """Unified move to the next step: value = text/preset to record, or None to skip."""
    cur = await state.get_state()
    if cur in _TEXT_STEPS_BY_NAME:
        field, next_state, question = _TEXT_STEPS_BY_NAME[cur]
        if value is not None:
            await _save_field(state, field, value)
        await state.set_state(next_state)
        await _persist_draft(session, user_id, state)
        await message.answer(question, reply_markup=await _step_markup(prefs, _FIELD_BY_STATE[next_state]))
    elif cur == BugForm.environment.state:
        if value is not None:
            await _save_field(state, "environment", value)
        await state.set_state(BugForm.attachments)
        await _persist_draft(session, user_id, state)
        await _ask_attachments(message, settings)
    elif cur == FeatureForm.implementation.state:
        if value is not None:
            await _save_field(state, "implementation", value)
        data = await state.get_data()
        await _show_preview(message, state, data)
        await _persist_draft(session, user_id, state)


@router.message(StateFilter(*_BUG_TEXT_STEPS, *_FEATURE_TEXT_STEPS))
async def text_step(
    message: Message, state: FSMContext, bot: Bot, prefs: Prefs, session: AsyncSession, settings: Settings
) -> None:
    field, _next, _q = _TEXT_STEPS_BY_NAME[await state.get_state()]
    value = await _read_text(message, field)
    if value is None:
        return
    assert message.from_user
    await _advance(message, state, bot, prefs, session, settings, message.from_user.id, value)


@router.message(FeatureForm.implementation)
async def feature_last_step(
    message: Message, state: FSMContext, bot: Bot, prefs: Prefs, session: AsyncSession, settings: Settings
) -> None:
    value = await _read_text(message, "implementation")
    if value is None:
        return
    assert message.from_user
    await _advance(message, state, bot, prefs, session, settings, message.from_user.id, value)


# ---------------------------------------------------------------- environment and attachments (bug)


@router.message(BugForm.environment)
async def bug_environment(
    message: Message, state: FSMContext, bot: Bot, prefs: Prefs, session: AsyncSession, settings: Settings
) -> None:
    value = await _read_text(message, "environment")
    if value is None:
        return
    assert message.from_user
    await _advance(message, state, bot, prefs, session, settings, message.from_user.id, value)


# ---------------------------------------------------------------- quick buttons and skipping a step


@router.callback_query(keyboards.QuickCb.filter())
async def quick_fill(
    callback: CallbackQuery,
    callback_data: keyboards.QuickCb,
    state: FSMContext,
    bot: Bot,
    prefs: Prefs,
    session: AsyncSession,
    settings: Settings,
) -> None:
    cur = await state.get_state()
    if _FIELD_BY_STATE_STR.get(cur) != callback_data.step:
        await callback.answer()
        return
    buttons = await prefs.list_buttons(callback_data.step)
    if not (0 <= callback_data.idx < len(buttons)):
        await callback.answer()
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
        await _advance(
            callback.message, state, bot, prefs, session, settings, callback.from_user.id, buttons[callback_data.idx]["value"]
        )


@router.callback_query(keyboards.FlowCb.filter(F.action == "skip"))
async def skip_step(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    prefs: Prefs,
    session: AsyncSession,
    settings: Settings,
) -> None:
    cur = await state.get_state()
    msg = callback.message if isinstance(callback.message, Message) else None
    if cur == BugForm.attachments.state:
        # Skipping the attachments step = "Done" with no attachments (only when attachments are optional).
        await _finish_attachments(callback, state, session, settings)
        return
    if cur not in _TEXT_STEPS_BY_NAME and cur not in (BugForm.environment.state, FeatureForm.implementation.state):
        await callback.answer()
        return
    await callback.answer(texts.SKIPPED)
    if msg is not None:
        await msg.edit_reply_markup(reply_markup=None)
        await _advance(msg, state, bot, prefs, session, settings, callback.from_user.id, None)


@router.message(BugForm.attachments)
async def bug_attachment(message: Message, state: FSMContext, settings: Settings, session: AsyncSession) -> None:
    assert message.from_user
    files = extract_files(message)
    async with _locks(message.from_user.id):
        data = await state.get_data()
        if files:
            data["files"] = data.get("files", []) + [asdict(f) for f in files]
            # A file caption is useful too — keep it with the logs.
            if message.caption:
                data.setdefault("fields", {}).setdefault("logs", []).append(message.caption[: reports.FIELD_LIMITS["log"]])
        elif message.text and not message.text.startswith("/"):
            text = message.text.strip()
            limit = reports.FIELD_LIMITS["log"]
            if len(text) > limit:
                await message.answer(texts.TOO_LONG.format(n=len(text), max=limit) + texts.LONG_LOG_HINT)
                return
            data.setdefault("fields", {}).setdefault("logs", []).append(text)
        else:
            await _ask_attachments(message, settings)
            return
        # An album arrives as a batch of messages — reply once per album, not per file.
        group = message.media_group_id
        should_reply = group is None or data.get("last_group") != group
        data["last_group"] = group
        await state.set_data(data)
        total = len(data["files"]) + len(data["fields"].get("logs", []))
    await _persist_draft(session, message.from_user.id, state)
    if should_reply:
        await message.answer(texts.ACCEPTED_ATTACHMENT.format(n=total), reply_markup=keyboards.done_kb())


async def _finish_attachments(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    data = await state.get_data()
    has_any = bool(data.get("files")) or bool(data.get("fields", {}).get("logs"))
    if settings.bug_attachments_required and not has_any:
        await callback.answer(texts.BUG_ATTACHMENTS_REQUIRED, show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
        await _show_preview(callback.message, state, data)
        await _persist_draft(session, callback.from_user.id, state)


@router.callback_query(BugForm.attachments, keyboards.FlowCb.filter(F.action == "done"))
async def bug_attachments_done(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    await _finish_attachments(callback, state, session, settings)


# ---------------------------------------------------------------- preview and submit


async def _show_preview(message: Message, state: FSMContext, data: dict) -> None:
    kind = data["kind"]
    await state.set_state(BugForm.confirm if kind == reports.KIND_BUG else FeatureForm.confirm)
    text = reports.preview_text(kind, data.get("fields", {}), attachments=len(data.get("files", [])))
    chunks = split_html_lines(text)
    for chunk in chunks[:-1]:
        await message.answer(chunk)
    await message.answer(chunks[-1], reply_markup=keyboards.confirm_kb())


async def _drop(message: Message | None) -> None:
    if message is not None:
        try:
            await message.delete()
        except TelegramAPIError:
            pass


@router.callback_query(
    StateFilter(BugForm.confirm, FeatureForm.confirm), keyboards.FlowCb.filter(F.action == "submit")
)
async def submit(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    summarizer: Summarizer,
    media: MediaDownloader,
    prefs: Prefs,
) -> None:
    user = callback.from_user
    async with _locks(user.id):
        data = await state.get_data()
        if await state.get_state() is None or not data:
            await callback.answer()
            return
        await state.clear()  # a second "Send" tap now does nothing
    # Answer right away: publishing with an LLM summary can take seconds, and the callback expires.
    await callback.answer(texts.PUBLISHING)

    kind = data["kind"]
    started = datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None
    files = [FileRef(**f) for f in data.get("files", [])]
    report, att_ids = await reports.create_report(
        session, kind=kind, user=user, fields=data.get("fields", {}), started_at=started, files=files
    )
    # The bug summary via LLM can take several seconds — warn the author.
    progress: Message | None = None
    if kind == reports.KIND_BUG and isinstance(summarizer, LLMSummarizer):
        progress = await bot.send_message(user.id, texts.LLM_PROCESSING.format(seconds=int(settings.llm_timeout)))
    try:
        await reports.publish(bot, report, settings, summarizer)
    except TelegramAPIError as exc:
        log.exception(texts.LOG["forms"]["publish_failed"])
        await session.rollback()
        await state.set_state(BugForm.confirm if kind == reports.KIND_BUG else FeatureForm.confirm)
        await state.set_data(data)
        await bot.send_message(user.id, texts.PUBLISH_FAILED.format(error=escape(exc.message)))
        await _drop(progress)
        return
    await drafts.mark_submitted(session, user.id)
    await session.commit()
    bus.publish({"type": "report", "id": report.public_id, "kind": kind})
    media.schedule(att_ids, subdir=f"reports/{report.public_id}")
    await _drop(progress)

    link = reports.report_post_link(report) or ""
    template = texts.SENT_BUG if kind == reports.KIND_BUG else texts.SENT_FEATURE
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(template.format(rid=report.public_id, link=link))

    title = texts.NEW_BUG_TITLE if kind == reports.KIND_BUG else texts.NEW_FEATURE_TITLE
    summary = escape(str(report.fields.get("summary") or report.fields.get("idea") or ""))[:300]
    event = "report_bug" if kind == reports.KIND_BUG else "report_feature"
    await announce(
        bot,
        settings,
        prefs,
        event,
        texts.REPORT_ANNOUNCE.format(
            title=title, rid=report.public_id, user=user_html_from(user), summary=summary, link=link
        ),
    )


@router.callback_query(
    StateFilter(BugForm.confirm, FeatureForm.confirm), keyboards.FlowCb.filter(F.action == "restart")
)
async def restart(
    callback: CallbackQuery, state: FSMContext, bot: Bot, settings: Settings, prefs: Prefs, session: AsyncSession
) -> None:
    data = await state.get_data()
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(texts.RESTART)
    await start_flow(bot, state, data.get("kind", reports.KIND_BUG), callback.from_user, session, settings, prefs)


@router.callback_query(keyboards.FlowCb.filter(F.action == "cancel"))
async def cancel_button(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    await drafts.mark_cancelled(session, callback.from_user.id)
    await callback.answer(texts.CANCELLED)
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
