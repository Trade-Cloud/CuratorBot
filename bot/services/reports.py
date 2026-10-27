"""Баг-репорты и предложения: сохранение, посты в топики, карточка «Подробнее»."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from html import escape
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    InlineKeyboardMarkup,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
    Message,
    User,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .. import texts
from ..config import Settings
from ..db.models import Attachment, Report
from ..keyboards import DetailsCb
from ..utils.ids import new_public_id
from ..utils.telegram import fmt_dt, full_name, post_link, split_html_lines, thread_param, user_html
from .llm import Summarizer
from .media import FileRef, attachment_from_ref

log = logging.getLogger(__name__)

KIND_BUG = "bug"
KIND_FEATURE = "feature"

# Лимиты полей анкеты: пост с предложением целиком должен влезть в одно сообщение (4096).
FIELD_LIMITS: dict[str, int] = {
    "description": 1000,
    "steps": 2500,
    "actual": 1500,
    "expected": 1500,
    "environment": 300,
    "log": 3500,
    "idea": 1000,
    "problem": 1200,
    "implementation": 1500,
}

BUG_FIELDS = (
    ("description", "Описание"),
    ("steps", "Шаги воспроизведения"),
    ("actual", "Фактический результат"),
    ("expected", "Ожидаемый результат"),
    ("environment", "Окружение"),
)
FEATURE_FIELDS = (
    ("idea", "Суть идеи"),
    ("problem", "Какую проблему решает"),
    ("implementation", "Ожидаемая реализация"),
)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def status_label(status: str) -> str:
    return texts.STATUS_LABELS.get(status, status)


def _fields_block(fields: dict[str, Any], spec: tuple[tuple[str, str], ...]) -> str:
    parts = []
    for key, title in spec:
        value = fields.get(key)
        if value:
            parts.append(f"<b>{title}:</b>\n{escape(str(value))}")
    return "\n\n".join(parts)


def preview_text(kind: str, fields: dict[str, Any], attachments: int = 0) -> str:
    spec = BUG_FIELDS if kind == KIND_BUG else FEATURE_FIELDS
    body = _fields_block(fields, spec)
    extra = ""
    if kind == KIND_BUG:
        logs = fields.get("logs") or []
        extra = f"\n\n📎 Вложений: {attachments}" + (f", логов текстом: {len(logs)}" if logs else "")
    return f"{texts.PREVIEW_HEADER}\n\n{body}{extra}"


# ---------------------------------------------------------------- создание


async def _unique_public_id(session: AsyncSession) -> str:
    for _ in range(20):
        candidate = new_public_id()
        exists = await session.scalar(select(Report.id).where(Report.public_id == candidate))
        if exists is None:
            return candidate
    raise RuntimeError("could not generate unique report id")


async def create_report(
    session: AsyncSession,
    *,
    kind: str,
    user: User,
    fields: dict[str, Any],
    started_at: datetime | None,
    files: list[FileRef] | None = None,
) -> tuple[Report, list[int]]:
    report = Report(
        public_id=await _unique_public_id(session),
        kind=kind,
        status="open",
        user_id=user.id,
        username=user.username,
        full_name=full_name(user),
        fields=fields,
        started_at=started_at,
    )
    session.add(report)
    await session.flush()
    rows = [attachment_from_ref(ref, report_id=report.id) for ref in files or []]
    session.add_all(rows)
    await session.flush()
    return report, [r.id for r in rows]


async def get_report(session: AsyncSession, public_id: str) -> Report | None:
    return (
        await session.execute(
            select(Report).options(selectinload(Report.attachments)).where(Report.public_id == public_id)
        )
    ).scalar_one_or_none()


def can_view(report: Report, user_id: int, settings: Settings) -> bool:
    return report.user_id == user_id or settings.is_admin(user_id)


# ---------------------------------------------------------------- посты в топики


def _created_line(report: Report, settings: Settings) -> str:
    created = report.created_at or datetime.now(timezone.utc)
    return (
        f"👤 {user_html(report.user_id, report.full_name, report.username)}\n"
        f"🕒 {fmt_dt(created, settings.timezone)}"
    )


async def bug_post_text(report: Report, settings: Settings, summarizer: Summarizer) -> str:
    # Аннотацию считаем один раз при публикации и храним — смена статуса не должна заново дёргать LLM.
    summary = report.fields.get("summary")
    if not summary:
        summary = await summarizer.summarize_bug(report.fields, settings.bug_summary_chars)
        report.fields = {**report.fields, "summary": summary}
    return (
        f"🐞 <b>Баг</b> <code>#{report.public_id}</code> · {status_label(report.status)}\n\n"
        f"{escape(summary)}\n\n"
        f"{_created_line(report, settings)}"
    )


def feature_post_text(report: Report, settings: Settings) -> str:
    return (
        f"💡 <b>Предложение</b> <code>#{report.public_id}</code> · {status_label(report.status)}\n\n"
        f"{_fields_block(report.fields, FEATURE_FIELDS)}\n\n"
        f"{_created_line(report, settings)}"
    )


def details_keyboard(public_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.DETAILS_BUTTON, callback_data=DetailsCb(rid=public_id))
    return kb.as_markup()


async def render_post(report: Report, settings: Settings, summarizer: Summarizer) -> tuple[str, InlineKeyboardMarkup | None]:
    if report.kind == KIND_BUG:
        return await bug_post_text(report, settings, summarizer), details_keyboard(report.public_id)
    return feature_post_text(report, settings), None


def topic_for(kind: str, settings: Settings) -> int | None:
    return settings.topic_bugs_id if kind == KIND_BUG else settings.topic_features_id


async def publish(bot: Bot, report: Report, settings: Settings, summarizer: Summarizer) -> Message:
    topic = topic_for(report.kind, settings)
    text, markup = await render_post(report, settings, summarizer)
    msg = await bot.send_message(
        settings.chat_id,
        text,
        message_thread_id=thread_param(topic),
        reply_markup=markup,
        link_preview_options=NO_PREVIEW,
    )
    report.post_chat_id = msg.chat.id
    report.post_thread_id = topic
    report.post_message_id = msg.message_id
    return msg


async def refresh_post(bot: Bot, report: Report, settings: Settings, summarizer: Summarizer) -> None:
    if not report.post_message_id or not report.post_chat_id:
        return
    text, markup = await render_post(report, settings, summarizer)
    try:
        await bot.edit_message_text(
            text=text,
            chat_id=report.post_chat_id,
            message_id=report.post_message_id,
            reply_markup=markup,
            link_preview_options=NO_PREVIEW,
        )
    except TelegramAPIError as exc:
        log.warning("cannot edit post for %s: %s", report.public_id, exc)


def report_post_link(report: Report) -> str | None:
    if not report.post_chat_id or not report.post_message_id:
        return None
    return post_link(report.post_chat_id, report.post_message_id, report.post_thread_id)


# ---------------------------------------------------------------- «Подробнее»


async def details_text(session: AsyncSession, report: Report, settings: Settings) -> str:
    total_by_author = await session.scalar(
        select(func.count(Report.id)).where(Report.user_id == report.user_id, Report.kind == report.kind)
    )
    created = report.created_at or datetime.now(timezone.utc)
    lines = [
        f"🐞 <b>Баг</b> <code>#{report.public_id}</code>" if report.kind == KIND_BUG
        else f"💡 <b>Предложение</b> <code>#{report.public_id}</code>",
        f"Статус: {status_label(report.status)}"
        + (f" — {escape(report.status_comment)}" if report.status_comment else ""),
        "",
        _fields_block(report.fields, BUG_FIELDS if report.kind == KIND_BUG else FEATURE_FIELDS),
    ]
    logs = report.fields.get("logs") or []
    for i, chunk in enumerate(logs, 1):
        lines += ["", f"<b>Лог {i}:</b>", f"<pre>{escape(str(chunk))}</pre>"]

    attachments = report.attachments
    saved = sum(1 for a in attachments if a.status == "done")
    stats = [
        f"Автор: {user_html(report.user_id, report.full_name, report.username)} · id <code>{report.user_id}</code>",
        f"Создан: {fmt_dt(created, settings.timezone)}",
        f"Обновлён: {fmt_dt(report.updated_at, settings.timezone)}" if report.updated_at else "",
        f"Заполнение анкеты: {_duration(report.started_at, created)}" if report.started_at else "",
        f"Вложений: {len(attachments)} (сохранено на диск: {saved})",
        f"Логов текстом: {len(logs)}" if logs else "",
        f"Всего {'багов' if report.kind == KIND_BUG else 'предложений'} от автора: {total_by_author}",
    ]
    lines += ["", "<b>Статистика</b>", *(s for s in stats if s)]
    link = report_post_link(report)
    if link:
        lines.append(f'<a href="{link}">Пост в чате</a>')
    return "\n".join(lines)


def _duration(start: datetime, end: datetime) -> str:
    seconds = max(0, int((end - start).total_seconds()))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин {sec} с"
    return f"{sec} с"


async def send_details(bot: Bot, chat_id: int, session: AsyncSession, report: Report, settings: Settings) -> None:
    text = await details_text(session, report, settings)
    for chunk in split_html_lines(text):
        await bot.send_message(chat_id, chunk, link_preview_options=NO_PREVIEW)
    await send_attachments(bot, chat_id, report.attachments)


async def send_attachments(bot: Bot, chat_id: int, attachments: list[Attachment]) -> None:
    """Альбомами по 10: фото+видео вместе, документы и аудио — отдельными альбомами
    (Telegram не даёт смешивать их с фото). Остальное — по одному через send_*."""
    visual = [a for a in attachments if a.kind in ("photo", "video")]
    docs = [a for a in attachments if a.kind == "document"]
    audio = [a for a in attachments if a.kind == "audio"]
    other = [a for a in attachments if a.kind not in ("photo", "video", "document", "audio")]

    async def album(items: list[Attachment], make) -> None:
        for i in range(0, len(items), 10):
            batch = items[i : i + 10]
            try:
                if len(batch) == 1:
                    await _send_single(bot, chat_id, batch[0])
                else:
                    await bot.send_media_group(chat_id, [make(a) for a in batch])
            except TelegramAPIError as exc:
                log.warning("send album failed: %s", exc)

    await album(
        visual,
        lambda a: InputMediaPhoto(media=a.file_id) if a.kind == "photo" else InputMediaVideo(media=a.file_id),
    )
    await album(docs, lambda a: InputMediaDocument(media=a.file_id))
    await album(audio, lambda a: InputMediaAudio(media=a.file_id))
    for att in other:
        try:
            await _send_single(bot, chat_id, att)
        except TelegramAPIError as exc:
            log.warning("send attachment %s failed: %s", att.id, exc)


async def _send_single(bot: Bot, chat_id: int, att: Attachment) -> None:
    senders = {
        "photo": bot.send_photo,
        "video": bot.send_video,
        "document": bot.send_document,
        "audio": bot.send_audio,
        "voice": bot.send_voice,
        "animation": bot.send_animation,
        "video_note": bot.send_video_note,
        "sticker": bot.send_sticker,
    }
    sender = senders.get(att.kind, bot.send_document)
    await sender(chat_id, att.file_id)
