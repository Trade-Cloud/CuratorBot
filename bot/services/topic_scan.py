"""Поиск топиков в чате беты.

В Bot API нет метода «список топиков», поэтому перебираем message_thread_id подряд и пробуем отправить пост.
ID топика = id служебного сообщения о его создании, так что это обычные числа (2, 5, 37, ...).
Если ID не топик, Telegram отвечает ошибкой и ничего не публикует; если топик — пост остаётся в нём с его ID.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import Message

from ..utils.telegram import GENERAL_TOPIC

log = logging.getLogger(__name__)

# Названия топиков -> переменная в .env (сравнение без регистра и пробелов).
ENV_BY_NAME = {
    "info": "TOPIC_INFO_ID",
    "changelog": "TOPIC_CHANGELOG_ID",
    "bugreports": "TOPIC_BUGS_ID",
    "bugs": "TOPIC_BUGS_ID",
    "featurerequests": "TOPIC_FEATURES_ID",
    "features": "TOPIC_FEATURES_ID",
    "main": "TOPIC_MAIN_ID",
    "general": "TOPIC_MAIN_ID",
}


@dataclass(slots=True)
class FoundTopic:
    thread_id: int
    name: str | None
    message_id: int

    @property
    def env_key(self) -> str | None:
        if not self.name:
            return None
        return ENV_BY_NAME.get("".join(self.name.lower().split()))


def _topic_name(msg: Message) -> str | None:
    """В ответе на отправку в топик reply_to_message — служебное сообщение о создании топика с его названием."""
    reply = msg.reply_to_message
    created = reply.forum_topic_created if reply else None
    return created.name if created else None


def _post_text(thread_id: int, name: str | None) -> str:
    title = f" «{escape(name)}»" if name else ""
    general = " (General)" if thread_id == GENERAL_TOPIC else ""
    return f"🧭 Топик{title}{general}\nmessage_thread_id: <code>{thread_id}</code>"


async def _send(bot: Bot, chat_id: int, thread_id: int | None, text: str) -> Message | None:
    while True:
        try:
            return await bot.send_message(chat_id, text, message_thread_id=thread_id)
        except TelegramRetryAfter as exc:
            log.info("flood wait %ss", exc.retry_after)
            await asyncio.sleep(exc.retry_after + 1)
        except TelegramBadRequest as exc:
            # "message thread not found" и подобное — значит, такого топика нет.
            log.debug("thread %s: %s", thread_id, exc.message)
            return None


async def scan_topics(
    bot: Bot,
    chat_id: int,
    max_thread_id: int = 300,
    known: set[int] | None = None,
    delay: float = 0.05,
) -> list[FoundTopic]:
    found: list[FoundTopic] = []

    general = await _send(bot, chat_id, None, _post_text(GENERAL_TOPIC, None))
    if general:
        found.append(FoundTopic(GENERAL_TOPIC, "General", general.message_id))

    # Сначала топики, которые бот уже видел (из БД), потом перебор.
    candidates = sorted(known or set()) + [i for i in range(2, max_thread_id + 1) if i not in (known or set())]
    seen_ids: set[int] = set()
    for thread_id in candidates:
        if thread_id in seen_ids or thread_id == GENERAL_TOPIC:
            continue
        seen_ids.add(thread_id)
        # Сначала пробный пост без названия, потом правим текст, когда узнали название.
        msg = await _send(bot, chat_id, thread_id, _post_text(thread_id, None))
        if msg is not None:
            name = _topic_name(msg)
            if name:
                try:
                    await msg.edit_text(_post_text(thread_id, name))
                except TelegramBadRequest:
                    pass
            found.append(FoundTopic(thread_id, name, msg.message_id))
            log.info("found topic %s %r", thread_id, name)
            # Ограничение Telegram — ~20 сообщений в минуту в одну группу.
            await asyncio.sleep(3)
        else:
            await asyncio.sleep(delay)
    return found


def env_snippet(found: list[FoundTopic]) -> str:
    lines = []
    for topic in found:
        key = topic.env_key
        if key:
            lines.append(f"{key}={topic.thread_id}")
    return "\n".join(lines)


def report_text(found: list[FoundTopic], max_thread_id: int) -> str:
    if not found:
        return (
            "Ни одного топика не нашёл. Проверь, что бот админ в чате, CHAT_ID верный и в чате включены темы."
        )
    rows = [
        f"• <code>{t.thread_id}</code> — {escape(t.name or 'без названия')}"
        + (f" → <code>{t.env_key}</code>" if t.env_key else "")
        for t in found
    ]
    text = f"Найдено топиков: {len(found)} (перебор до {max_thread_id})\n" + "\n".join(rows)
    snippet = env_snippet(found)
    if snippet:
        text += f"\n\nДля .env:\n<pre>{escape(snippet)}</pre>"
    return text


# ---------------------------------------------------------------- проверка топиков из .env

CONFIGURED_TOPICS = (
    ("topic_info_id", "INFO"),
    ("topic_changelog_id", "Changelog"),
    ("topic_bugs_id", "Bug Reports"),
    ("topic_features_id", "Feature Requests"),
    ("topic_main_id", "Main"),
)


async def test_configured_topics(bot: Bot, settings) -> list[str]:
    """Кидает «Топик <id> - <name>» в каждый топик из .env. Возвращает строки отчёта."""
    results: list[str] = []
    for attr, label in CONFIGURED_TOPICS:
        topic_id = getattr(settings, attr)
        env_key = attr.upper()
        if topic_id is None:
            results.append(f"⚪️ {env_key}: не задан")
            continue
        try:
            msg = await bot.send_message(
                settings.chat_id, f"Топик {topic_id} - {escape(label)}", message_thread_id=topic_id or None
            )
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
            msg = await bot.send_message(
                settings.chat_id, f"Топик {topic_id} - {escape(label)}", message_thread_id=topic_id or None
            )
        except TelegramBadRequest as exc:
            results.append(f"❌ {env_key}={topic_id}: {escape(exc.message)}")
            continue
        # Реальное название берём из чата: оно в служебном сообщении о создании топика.
        name = "General" if topic_id == GENERAL_TOPIC else _topic_name(msg)
        if name and name != label:
            try:
                await msg.edit_text(f"Топик {topic_id} - {escape(name)}")
            except TelegramBadRequest:
                pass
        mismatch = f" (в .env это {label})" if name and name.lower() != label.lower() and topic_id else ""
        results.append(f"✅ {env_key}={topic_id} - {escape(name or label)}{mismatch}")
    return results
