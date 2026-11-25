"""Bug reports and feature requests: storage, topic posts, the "Details" card."""

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
from ..config import CONFIG, Settings
from ..db.models import Attachment, Report
from ..keyboards import DetailsCb
from ..utils.ids import new_public_id
from ..utils.telegram import fmt_dt, full_name, post_link, split_html_lines, thread_param, user_html
from .llm import Summarizer
from .media import FileRef, attachment_from_ref

log = logging.getLogger(__name__)

KIND_BUG = "bug"
KIND_FEATURE = "feature"

# Form field limits: a full feature-request post must fit into one message (4096).
FIELD_LIMITS: dict[str, int] = CONFIG["reports"]["field_limits"]

BUG_FIELDS = tuple(texts.FIELDS_BUG.items())
FEATURE_FIELDS = tuple(texts.FIELDS_FEATURE.items())
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def status_label(status: str) -> str:
    return texts.STATUS_LABELS.get(status, status)


def _fields_block(fields: dict[str, Any], spec: tuple[tuple[str, str], ...]) -> str:
    parts = []
    for key, title in spec:
        value = fields.get(key)
        if value:
            parts.append(texts.FIELD_BLOCK.format(title=title, value=escape(str(value))))
    return "\n\n".join(parts)


def preview_text(kind: str, fields: dict[str, Any], attachments: int = 0) -> str:
    spec = BUG_FIELDS if kind == KIND_BUG else FEATURE_FIELDS
    body = _fields_block(fields, spec)
    extra = ""
    if kind == KIND_BUG:
        logs = fields.get("logs") or []
        extra = texts.PREVIEW_ATTACHMENTS.format(n=attachments) + (texts.PREVIEW_LOGS.format(n=len(logs)) if logs else "")
    return f"{texts.PREVIEW_HEADER}\n\n{body}{extra}"


# ---------------------------------------------------------------- creation


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


def edit_field(report: Report, field: str, value: str) -> None:
    """Write a new value for a form field. For bugs, drop the AI summary so the post
    recomputes it from the updated fields."""
    fields = dict(report.fields)
    fields[field] = value
    if report.kind == KIND_BUG:
        fields.pop("summary", None)
    report.fields = fields


# ---------------------------------------------------------------- topic posts


def _created_line(report: Report, settings: Settings) -> str:
    created = report.created_at or datetime.now(timezone.utc)
    return texts.CREATED_LINE.format(
        user=user_html(report.user_id, report.full_name, report.username),
        dt=fmt_dt(created, settings.timezone),
    )


async def bug_post_text(report: Report, settings: Settings, summarizer: Summarizer) -> str:
    # Compute the summary once at publish time and store it — a status change must not re-invoke the LLM.
    summary = report.fields.get("summary")
    if not summary:
        summary = await summarizer.summarize_bug(report.fields, settings.bug_summary_chars)
        report.fields = {**report.fields, "summary": summary}
    return texts.POST_BUG.format(
        rid=report.public_id,
        status=status_label(report.status),
        summary=escape(summary),
        created=_created_line(report, settings),
    )


def feature_post_text(report: Report, settings: Settings) -> str:
    return texts.POST_FEATURE.format(
        rid=report.public_id,
        status=status_label(report.status),
        body=_fields_block(report.fields, FEATURE_FIELDS),
        created=_created_line(report, settings),
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
        log.warning(texts.LOG["reports"]["edit_post_failed"], report.public_id, exc)


def report_post_link(report: Report) -> str | None:
    if not report.post_chat_id or not report.post_message_id:
        return None
    return post_link(report.post_chat_id, report.post_message_id, report.post_thread_id)


async def apply_status(
    bot: Bot,
    session: AsyncSession,
    report: Report,
    status: str,
    comment: str | None,
    settings: Settings,
    summarizer: Summarizer,
) -> None:
    """Change a report's status: update the chat post and notify the author. Shared by command and web."""
    report.status = status
    report.status_comment = comment or None
    report.updated_at = datetime.now(timezone.utc)
    await session.flush()
    await refresh_post(bot, report, settings, summarizer)
    label = status_label(report.status)
    author_comment = f"\n{escape(report.status_comment)}" if report.status_comment else ""
    kindword = texts.KINDWORD_FEATURE if report.kind == KIND_FEATURE else texts.KINDWORD_BUG
    try:
        await bot.send_message(
            report.user_id,
            texts.STATUS_CHANGED_AUTHOR.format(kind=kindword, rid=report.public_id, status=label, comment=author_comment),
        )
    except TelegramAPIError:
        pass


# ---------------------------------------------------------------- "Details"


async def details_text(session: AsyncSession, report: Report, settings: Settings) -> str:
    total_by_author = await session.scalar(
        select(func.count(Report.id)).where(Report.user_id == report.user_id, Report.kind == report.kind)
    )
    created = report.created_at or datetime.now(timezone.utc)
    header = (
        texts.DETAILS_HEADER_BUG.format(rid=report.public_id) if report.kind == KIND_BUG
        else texts.DETAILS_HEADER_FEATURE.format(rid=report.public_id)
    )
    status_line = texts.DETAILS_STATUS.format(status=status_label(report.status)) + (
        texts.DETAILS_STATUS_COMMENT.format(comment=escape(report.status_comment)) if report.status_comment else ""
    )
    lines = [
        header,
        status_line,
        "",
        _fields_block(report.fields, BUG_FIELDS if report.kind == KIND_BUG else FEATURE_FIELDS),
    ]
    logs = report.fields.get("logs") or []
    for i, chunk in enumerate(logs, 1):
        lines += ["", texts.DETAILS_LOG_HEADER.format(i=i), texts.DETAILS_LOG_BODY.format(value=escape(str(chunk)))]

    attachments = report.attachments
    saved = sum(1 for a in attachments if a.status == "done")
    total_kind = texts.DETAILS_TOTAL_KIND_BUG if report.kind == KIND_BUG else texts.DETAILS_TOTAL_KIND_FEATURE
    stats = [
        texts.DETAILS_AUTHOR.format(
            user=user_html(report.user_id, report.full_name, report.username), user_id=report.user_id
        ),
        texts.DETAILS_CREATED.format(dt=fmt_dt(created, settings.timezone)),
        texts.DETAILS_UPDATED.format(dt=fmt_dt(report.updated_at, settings.timezone)) if report.updated_at else "",
        texts.DETAILS_DURATION.format(duration=_duration(report.started_at, created)) if report.started_at else "",
        texts.DETAILS_ATTACHMENTS.format(total=len(attachments), saved=saved),
        texts.DETAILS_TEXT_LOGS.format(n=len(logs)) if logs else "",
        texts.DETAILS_TOTAL.format(kind=total_kind, n=total_by_author),
    ]
    lines += ["", texts.DETAILS_STATS_HEADER, *(s for s in stats if s)]
    link = report_post_link(report)
    if link:
        lines.append(texts.DETAILS_POST_LINK.format(link=link))
    return "\n".join(lines)


def _duration(start: datetime, end: datetime) -> str:
    seconds = max(0, int((end - start).total_seconds()))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return texts.DURATION_HM.format(h=hours, m=minutes)
    if minutes:
        return texts.DURATION_MS.format(m=minutes, s=sec)
    return texts.DURATION_S.format(s=sec)


async def send_details(bot: Bot, chat_id: int, session: AsyncSession, report: Report, settings: Settings) -> None:
    text = await details_text(session, report, settings)
    for chunk in split_html_lines(text):
        await bot.send_message(chat_id, chunk, link_preview_options=NO_PREVIEW)
    await send_attachments(bot, chat_id, report.attachments)


async def send_attachments(bot: Bot, chat_id: int, attachments: list[Attachment]) -> None:
    """Albums of 10: photos+videos together; documents and audio as separate albums
    (Telegram does not allow mixing them with photos). Everything else goes one by one via send_*."""
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
                log.warning(texts.LOG["reports"]["album_failed"], exc)

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
            log.warning(texts.LOG["reports"]["attachment_failed"], att.id, exc)


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
