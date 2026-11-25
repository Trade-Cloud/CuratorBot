"""Discovering topics in the beta chat.

The Bot API has no "list topics" method, so we iterate message_thread_id values and try to post.
A topic id equals the id of its creation service message, so these are plain numbers (2, 5, 37, ...).
If an id is not a topic, Telegram returns an error and posts nothing; if it is, the post stays with its id.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import Message

from .. import texts
from ..config import CONFIG
from ..utils.telegram import GENERAL_TOPIC

log = logging.getLogger(__name__)

# Topic names -> .env variable (compared case- and whitespace-insensitively).
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
    """In the reply to a topic post, reply_to_message is the topic-creation service message with its name."""
    reply = msg.reply_to_message
    created = reply.forum_topic_created if reply else None
    return created.name if created else None


def _post_text(thread_id: int, name: str | None) -> str:
    title = texts.SCAN_POST_TITLE.format(name=escape(name)) if name else ""
    general = texts.SCAN_POST_GENERAL if thread_id == GENERAL_TOPIC else ""
    return texts.SCAN_POST.format(title=title, general=general, thread_id=thread_id)


async def _send(bot: Bot, chat_id: int, thread_id: int | None, text: str) -> Message | None:
    while True:
        try:
            return await bot.send_message(chat_id, text, message_thread_id=thread_id)
        except TelegramRetryAfter as exc:
            log.info(texts.LOG["topic_scan"]["flood_wait"], exc.retry_after)
            await asyncio.sleep(exc.retry_after + 1)
        except TelegramBadRequest as exc:
            # "message thread not found" and the like — there is no such topic.
            log.debug(texts.LOG["topic_scan"]["thread_skip"], thread_id, exc.message)
            return None


async def scan_topics(
    bot: Bot,
    chat_id: int,
    max_thread_id: int = CONFIG["topic_scan"]["max_thread_id"],
    known: set[int] | None = None,
    delay: float = CONFIG["topic_scan"]["skip_delay"],
    post_delay: float = CONFIG["topic_scan"]["post_delay"],
) -> list[FoundTopic]:
    found: list[FoundTopic] = []

    general = await _send(bot, chat_id, None, _post_text(GENERAL_TOPIC, None))
    if general:
        found.append(FoundTopic(GENERAL_TOPIC, "General", general.message_id))

    # First the topics the bot has already seen (from the DB), then the sweep.
    candidates = sorted(known or set()) + [i for i in range(2, max_thread_id + 1) if i not in (known or set())]
    seen_ids: set[int] = set()
    for thread_id in candidates:
        if thread_id in seen_ids or thread_id == GENERAL_TOPIC:
            continue
        seen_ids.add(thread_id)
        # First a trial post without a name, then rewrite the text once the name is known.
        msg = await _send(bot, chat_id, thread_id, _post_text(thread_id, None))
        if msg is not None:
            name = _topic_name(msg)
            if name:
                try:
                    await msg.edit_text(_post_text(thread_id, name))
                except TelegramBadRequest:
                    pass
            found.append(FoundTopic(thread_id, name, msg.message_id))
            log.info(texts.LOG["topic_scan"]["found"], thread_id, name)
            # Telegram limit — ~20 messages per minute into a single group.
            await asyncio.sleep(post_delay)
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
        return texts.SCAN_NONE
    rows = [
        texts.SCAN_ROW.format(thread_id=t.thread_id, name=escape(t.name or texts.SCAN_ROW_UNNAMED))
        + (texts.SCAN_ROW_ENV.format(env_key=t.env_key) if t.env_key else "")
        for t in found
    ]
    text = texts.SCAN_HEADER.format(n=len(found), max=max_thread_id) + "\n" + "\n".join(rows)
    snippet = env_snippet(found)
    if snippet:
        text += texts.SCAN_ENV_BLOCK.format(snippet=escape(snippet))
    return text


# ---------------------------------------------------------------- checking the .env topics

CONFIGURED_TOPICS = tuple(texts.CONFIGURED_TOPICS.items())


async def test_configured_topics(bot: Bot, settings) -> list[str]:
    """Post "Topic <id> - <name>" into every topic from .env. Returns report lines."""
    results: list[str] = []
    for attr, label in CONFIGURED_TOPICS:
        topic_id = getattr(settings, attr)
        env_key = attr.upper()
        if topic_id is None:
            results.append(texts.TEST_NOT_SET.format(env_key=env_key))
            continue
        try:
            msg = await bot.send_message(
                settings.chat_id,
                texts.TEST_POST.format(thread_id=topic_id, name=escape(label)),
                message_thread_id=topic_id or None,
            )
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
            msg = await bot.send_message(
                settings.chat_id,
                texts.TEST_POST.format(thread_id=topic_id, name=escape(label)),
                message_thread_id=topic_id or None,
            )
        except TelegramBadRequest as exc:
            results.append(texts.TEST_ERROR.format(env_key=env_key, thread_id=topic_id, error=escape(exc.message)))
            continue
        # The real name comes from the chat: it is in the topic-creation service message.
        name = texts.GENERAL_NAME if topic_id == GENERAL_TOPIC else _topic_name(msg)
        if name and name != label:
            try:
                await msg.edit_text(texts.TEST_POST.format(thread_id=topic_id, name=escape(name)))
            except TelegramBadRequest:
                pass
        mismatch = texts.TEST_MISMATCH.format(label=label) if name and name.lower() != label.lower() and topic_id else ""
        results.append(
            texts.TEST_OK.format(env_key=env_key, thread_id=topic_id, name=escape(name or label), mismatch=mismatch)
        )
    return results
