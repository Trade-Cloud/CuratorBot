"""Анкеты в ЛС: баг-репорт и предложение."""

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
from aiogram.types import CallbackQuery, Message, User
from sqlalchemy.ext.asyncio import AsyncSession

from .. import keyboards, texts
from ..config import Settings
from ..services import reports
from ..services.llm import LLMSummarizer, Summarizer
from ..services.media import FileRef, MediaDownloader, extract_files
from ..services.member_tags import MemberTagResolver
from ..services.notify import notify_admins
from ..states import BugForm, FeatureForm
from ..web.events import bus
from ..utils.telegram import KeyedLock, split_html_lines, user_html_from

log = logging.getLogger(__name__)

router = Router(name="forms")
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

_locks = KeyedLock()

# Шаг -> (поле, следующий шаг, вопрос для следующего шага)
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


# ---------------------------------------------------------------- старт анкеты (используется и из группы)


async def start_flow(bot: Bot, state: FSMContext, chat_id: int, kind: str) -> None:
    """Шлёт первый вопрос в ЛС и ставит состояние. Бросает TelegramForbiddenError,
    если пользователь ещё не запускал бота — тогда вызывающий даёт ему deep-link."""
    if kind == reports.KIND_BUG:
        await bot.send_message(chat_id, texts.BUG_INTRO)
        await bot.send_message(chat_id, texts.BUG_ASK_DESCRIPTION)
        first = BugForm.description
    else:
        await bot.send_message(chat_id, texts.FEATURE_INTRO)
        await bot.send_message(chat_id, texts.FEATURE_ASK_IDEA)
        first = FeatureForm.idea
    await state.set_state(first)
    await state.set_data(
        {
            "kind": kind,
            "fields": {},
            "files": [],
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
    )


async def check_can_report(bot: Bot, user: User, settings: Settings, tags: MemberTagResolver) -> str | None:
    """Текст отказа или None, если можно."""
    if settings.require_membership and not settings.is_admin(user.id):
        if not await tags.is_member(bot, settings.chat_id, user.id):
            return texts.NOT_A_MEMBER
    return None


async def begin_from_private(
    message: Message, state: FSMContext, bot: Bot, settings: Settings, tags: MemberTagResolver, kind: str
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
    await start_flow(bot, state, message.chat.id, kind)


@router.message(Command("bug_report", "bug"))
async def cmd_bug(message: Message, state: FSMContext, bot: Bot, settings: Settings, tags: MemberTagResolver) -> None:
    await begin_from_private(message, state, bot, settings, tags, reports.KIND_BUG)


@router.message(Command("feature"))
async def cmd_feature(message: Message, state: FSMContext, bot: Bot, settings: Settings, tags: MemberTagResolver) -> None:
    await begin_from_private(message, state, bot, settings, tags, reports.KIND_FEATURE)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer(texts.NOTHING_TO_CANCEL)
        return
    await state.clear()
    await message.answer(texts.CANCELLED)


# ---------------------------------------------------------------- текстовые шаги


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


@router.message(StateFilter(*_BUG_TEXT_STEPS, *_FEATURE_TEXT_STEPS))
async def text_step(message: Message, state: FSMContext) -> None:
    field, next_state, question = _TEXT_STEPS_BY_NAME[await state.get_state()]
    value = await _read_text(message, field)
    if value is None:
        return
    await _save_field(state, field, value)
    await state.set_state(next_state)
    await message.answer(question, reply_markup=keyboards.env_kb() if next_state == BugForm.environment else None)


@router.message(FeatureForm.implementation)
async def feature_last_step(message: Message, state: FSMContext) -> None:
    value = await _read_text(message, "implementation")
    if value is None:
        return
    data = await _save_field(state, "implementation", value)
    await _show_preview(message, state, data)


# ---------------------------------------------------------------- окружение и вложения (баг)


async def _set_environment(message: Message, state: FSMContext, value: str) -> None:
    await _save_field(state, "environment", value)
    await state.set_state(BugForm.attachments)
    await message.answer(texts.BUG_ASK_ATTACHMENTS, reply_markup=keyboards.done_kb())


@router.message(BugForm.environment)
async def bug_environment(message: Message, state: FSMContext) -> None:
    value = await _read_text(message, "environment")
    if value is not None:
        await _set_environment(message, state, value)


@router.callback_query(BugForm.environment, keyboards.FlowCb.filter(F.action == "env_default"))
async def bug_environment_default(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
        await _set_environment(callback.message, state, texts.ENV_DEFAULT)


@router.message(BugForm.attachments)
async def bug_attachment(message: Message, state: FSMContext) -> None:
    assert message.from_user
    files = extract_files(message)
    async with _locks(message.from_user.id):
        data = await state.get_data()
        if files:
            data["files"] = data.get("files", []) + [asdict(f) for f in files]
            # Подпись к файлу тоже полезна — кладём её в логи.
            if message.caption:
                data.setdefault("fields", {}).setdefault("logs", []).append(message.caption[: reports.FIELD_LIMITS["log"]])
        elif message.text and not message.text.startswith("/"):
            text = message.text.strip()
            limit = reports.FIELD_LIMITS["log"]
            if len(text) > limit:
                await message.answer(texts.TOO_LONG.format(n=len(text), max=limit) + "\nДлинный лог лучше прислать файлом.")
                return
            data.setdefault("fields", {}).setdefault("logs", []).append(text)
        else:
            await message.answer(texts.BUG_ASK_ATTACHMENTS, reply_markup=keyboards.done_kb())
            return
        # Альбом приходит пачкой сообщений — отвечаем один раз на альбом, а не на каждый файл.
        group = message.media_group_id
        should_reply = group is None or data.get("last_group") != group
        data["last_group"] = group
        await state.set_data(data)
        total = len(data["files"]) + len(data["fields"].get("logs", []))
    if should_reply:
        await message.answer(texts.ACCEPTED_ATTACHMENT.format(n=total), reply_markup=keyboards.done_kb())


@router.callback_query(BugForm.attachments, keyboards.FlowCb.filter(F.action == "done"))
async def bug_attachments_done(callback: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    has_any = bool(data.get("files")) or bool(data.get("fields", {}).get("logs"))
    if settings.bug_attachments_required and not has_any:
        await callback.answer(texts.BUG_ATTACHMENTS_REQUIRED, show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
        await _show_preview(callback.message, state, data)


# ---------------------------------------------------------------- превью и отправка


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
) -> None:
    user = callback.from_user
    async with _locks(user.id):
        data = await state.get_data()
        if await state.get_state() is None or not data:
            await callback.answer()
            return
        await state.clear()  # повторное нажатие «Отправить» уже ничего не сделает
    # Отвечаем сразу: публикация с LLM-аннотацией может занять секунды, а callback протухает.
    await callback.answer("Публикую…")

    kind = data["kind"]
    started = datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None
    files = [FileRef(**f) for f in data.get("files", [])]
    report, att_ids = await reports.create_report(
        session, kind=kind, user=user, fields=data.get("fields", {}), started_at=started, files=files
    )
    # Аннотация бага через LLM может идти несколько секунд — предупреждаем автора.
    progress: Message | None = None
    if kind == reports.KIND_BUG and isinstance(summarizer, LLMSummarizer):
        progress = await bot.send_message(user.id, texts.LLM_PROCESSING.format(seconds=int(settings.llm_timeout)))
    try:
        await reports.publish(bot, report, settings, summarizer)
    except TelegramAPIError as exc:
        log.exception("publish failed")
        await session.rollback()
        await state.set_state(BugForm.confirm if kind == reports.KIND_BUG else FeatureForm.confirm)
        await state.set_data(data)
        await bot.send_message(user.id, f"Не получилось опубликовать: {escape(exc.message)}. Попробуй «Отправить» ещё раз.")
        await _drop(progress)
        return
    await session.commit()
    bus.publish({"type": "report", "id": report.public_id, "kind": kind})
    media.schedule(att_ids, subdir=f"reports/{report.public_id}")
    await _drop(progress)

    link = reports.report_post_link(report) or ""
    template = texts.SENT_BUG if kind == reports.KIND_BUG else texts.SENT_FEATURE
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(template.format(rid=report.public_id, link=link))

    title = "🐞 Новый баг" if kind == reports.KIND_BUG else "💡 Новое предложение"
    summary = escape(str(report.fields.get("summary") or report.fields.get("idea") or ""))[:300]
    await notify_admins(
        bot,
        settings,
        f"{title} <code>#{report.public_id}</code> от {user_html_from(user)}\n{summary}\n{link}",
    )


@router.callback_query(
    StateFilter(BugForm.confirm, FeatureForm.confirm), keyboards.FlowCb.filter(F.action == "restart")
)
async def restart(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(texts.RESTART)
    await start_flow(bot, state, callback.from_user.id, data.get("kind", reports.KIND_BUG))


@router.callback_query(keyboards.FlowCb.filter(F.action == "cancel"))
async def cancel_button(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer(texts.CANCELLED)
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
