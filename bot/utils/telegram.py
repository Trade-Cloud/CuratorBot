from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message, User

log = logging.getLogger(__name__)

GENERAL_TOPIC = 0
MESSAGE_LIMIT = 4096

_background: set[asyncio.Task] = set()


def topic_of(message: Message) -> int:
    """ID топика сообщения. В General message_thread_id либо нет, либо это id ответа — поэтому смотрим is_topic_message."""
    if message.is_topic_message and message.message_thread_id:
        return message.message_thread_id
    return GENERAL_TOPIC


def thread_param(topic_id: int | None) -> int | None:
    """Значение message_thread_id для отправки: в General его передавать не надо."""
    return topic_id or None


def full_name(user: User) -> str:
    return " ".join(p for p in (user.first_name, user.last_name) if p) or str(user.id)


def user_html(user_id: int, name: str | None, username: str | None = None) -> str:
    link = f'<a href="tg://user?id={user_id}">{escape(name or str(user_id))}</a>'
    return f"{link} (@{escape(username)})" if username else link


def user_html_from(user: User) -> str:
    return user_html(user.id, full_name(user), user.username)


def post_link(chat_id: int, message_id: int, thread_id: int | None = None) -> str | None:
    """Ссылка на сообщение в приватной супергруппе (t.me/c/...)."""
    raw = str(chat_id)
    if not raw.startswith("-100"):
        return None
    internal = raw[4:]
    if thread_id:
        return f"https://t.me/c/{internal}/{thread_id}/{message_id}"
    return f"https://t.me/c/{internal}/{message_id}"


def fmt_dt(dt: datetime, tz: str) -> str:
    zone = ZoneInfo(tz)
    local = dt.astimezone(zone)
    return local.strftime("%d.%m.%Y %H:%M ") + (local.tzname() or "")


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip() + "…"


def split_html_lines(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Режет длинный текст по строкам, чтобы влезть в лимит Telegram.
    Блоки формируем сами и теги не переносим через границу строк, поэтому резать по \\n безопасно."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def spawn(coro) -> asyncio.Task:
    """Фоновая задача с сильной ссылкой, чтобы её не собрал GC."""
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


async def _delete_later(bot: Bot, chat_id: int, message_id: int, delay: int) -> None:
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        pass


def delete_later(message: Message, delay: int) -> None:
    if delay > 0 and message.bot:
        spawn(_delete_later(message.bot, message.chat.id, message.message_id, delay))


class KeyedLock:
    """asyncio.Lock на ключ: апдейты обрабатываются параллельно, а альбомы приходят пачкой."""

    def __init__(self) -> None:
        self._locks: defaultdict[object, asyncio.Lock] = defaultdict(asyncio.Lock)

    def __call__(self, key: object) -> asyncio.Lock:
        return self._locks[key]
