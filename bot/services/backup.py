"""Бекап чата: каждое сообщение, правка и вложение.

Ограничения Bot API, которые обойти нельзя:
- удаления сообщений ботам не приходят (видим только то, что удалили мы сами);
- без своего Bot API сервера файлы больше 20 МБ не скачиваются (сохраняем file_id и метаданные);
- бот должен быть админом чата, иначе в топиках он видит не всё.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from aiogram.types import Message, Update, User
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Attachment, ChatMessage, ChatUser, MessageVersion, RawUpdate, Topic
from ..utils.telegram import topic_of
from .media import attachment_from_ref, extract_files

log = logging.getLogger(__name__)


def dump(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, list):
        return [dump(x) for x in obj]
    return obj.model_dump(mode="json", exclude_none=True)


def update_chat_id(update: Update) -> int | None:
    event = update.event
    chat = getattr(event, "chat", None)
    if chat is None:
        message = getattr(event, "message", None)  # callback_query
        chat = getattr(message, "chat", None)
    return getattr(chat, "id", None)


def message_text(message: Message) -> tuple[str | None, str | None, list[Any] | None]:
    """(plain, html, entities) — для обычного текста и для подписей к медиа."""
    if message.text is not None:
        return message.text, message.html_text, dump(message.entities)
    if message.caption is not None:
        return message.caption, message.html_text, dump(message.caption_entities)
    return None, None, None


async def save_raw_update(session: AsyncSession, update: Update) -> None:
    session.add(
        RawUpdate(
            update_id=update.update_id,
            event_type=update.event_type,
            chat_id=update_chat_id(update),
            payload=dump(update),
        )
    )


async def upsert_user(session: AsyncSession, user: User, tag: str | None = None) -> None:
    values = dict(
        id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        is_bot=user.is_bot,
        language_code=user.language_code,
        last_seen=datetime.now(timezone.utc),
    )
    if tag is not None:
        values["last_tag"] = tag
    if session.bind.dialect.name == "postgresql":
        stmt = pg_insert(ChatUser).values(**values)
        update_cols = {k: stmt.excluded[k] for k in values if k != "id"}
        await session.execute(stmt.on_conflict_do_update(index_elements=[ChatUser.id], set_=update_cols))
        return
    existing = await session.get(ChatUser, user.id)
    if existing is None:
        session.add(ChatUser(**values))
    else:
        for key, value in values.items():
            setattr(existing, key, value)


async def save_topic_event(session: AsyncSession, message: Message) -> None:
    """Запоминаем названия топиков, когда видим их создание или переименование."""
    created = message.forum_topic_created
    edited = message.forum_topic_edited
    if not (created or edited):
        return
    thread_id = topic_of(message)
    row = (
        await session.execute(select(Topic).where(Topic.chat_id == message.chat.id, Topic.thread_id == thread_id))
    ).scalar_one_or_none()
    if row is None:
        row = Topic(chat_id=message.chat.id, thread_id=thread_id)
        session.add(row)
    if created:
        row.name = created.name
        row.icon_color = created.icon_color
    elif edited and edited.name:
        row.name = edited.name


def _reply_chat_id(message: Message) -> int | None:
    if message.reply_to_message:
        return message.reply_to_message.chat.id
    if message.external_reply and message.external_reply.chat:
        return message.external_reply.chat.id
    return None


async def save_message(session: AsyncSession, message: Message) -> list[int]:
    """Новое сообщение. Возвращает id вложений, которые надо скачать."""
    text, html_text, entities = message_text(message)
    reply = message.reply_to_message
    row = ChatMessage(
        chat_id=message.chat.id,
        message_id=message.message_id,
        thread_id=topic_of(message),
        user_id=message.from_user.id if message.from_user else None,
        sender_chat_id=message.sender_chat.id if message.sender_chat else None,
        sender_tag=message.sender_tag,
        date=message.date,
        content_type=message.content_type,
        text=text,
        html_text=html_text,
        entities=entities,
        reply_to_message_id=reply.message_id if reply else None,
        reply_to_chat_id=_reply_chat_id(message),
        quote_text=message.quote.text if message.quote else None,
        forward_origin=dump(message.forward_origin),
        media_group_id=message.media_group_id,
        via_bot_id=message.via_bot.id if message.via_bot else None,
        raw=dump(message),
    )
    session.add(row)
    await session.flush()
    return await _add_attachments(session, message, row.id)


async def save_edit(session: AsyncSession, message: Message) -> list[int]:
    """Правка: пишем версию со старым и новым текстом, обновляем текущий текст."""
    row = (
        await session.execute(
            select(ChatMessage).where(
                ChatMessage.chat_id == message.chat.id, ChatMessage.message_id == message.message_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        # Сообщение было до запуска бота — сохраняем как есть, старого текста у нас нет.
        att_ids = await save_message(session, message)
        row = (
            await session.execute(
                select(ChatMessage).where(
                    ChatMessage.chat_id == message.chat.id, ChatMessage.message_id == message.message_id
                )
            )
        ).scalar_one()
    else:
        att_ids = []

    text, html_text, entities = message_text(message)
    last_version = await session.scalar(
        select(func.coalesce(func.max(MessageVersion.version), 0)).where(MessageVersion.message_pk == row.id)
    )
    edit_date = message.edit_date
    session.add(
        MessageVersion(
            message_pk=row.id,
            version=(last_version or 0) + 1,
            old_text=row.text,
            new_text=text,
            old_entities=row.entities,
            new_entities=entities,
            edit_date=edit_date,
            raw=dump(message),
        )
    )
    row.text = text
    row.html_text = html_text
    row.entities = entities
    row.content_type = message.content_type
    row.is_edited = True
    row.edit_count = (row.edit_count or 0) + 1
    row.last_edit_date = edit_date
    row.raw = dump(message)

    # Правкой могут заменить медиа — сохраняем новый файл, если его ещё не было.
    if not att_ids:
        att_ids = await _add_attachments(session, message, row.id, skip_known=True)
    return att_ids


async def mark_deleted(session: AsyncSession, chat_id: int, message_id: int) -> None:
    row = (
        await session.execute(
            select(ChatMessage).where(ChatMessage.chat_id == chat_id, ChatMessage.message_id == message_id)
        )
    ).scalar_one_or_none()
    if row is not None:
        row.deleted_by_bot = True


async def _add_attachments(session: AsyncSession, message: Message, message_pk: int, skip_known: bool = False) -> list[int]:
    refs = extract_files(message)
    if not refs:
        return []
    known: set[str] = set()
    if skip_known:
        known = set(
            (
                await session.execute(
                    select(Attachment.file_unique_id).where(Attachment.message_pk == message_pk)
                )
            ).scalars()
        )
    rows = [attachment_from_ref(ref, message_pk=message_pk) for ref in refs if ref.file_unique_id not in known]
    session.add_all(rows)
    await session.flush()
    return [r.id for r in rows]
