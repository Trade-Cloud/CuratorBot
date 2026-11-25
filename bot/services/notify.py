from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import LinkPreviewOptions

from .. import texts
from ..config import Settings
from ..utils.telegram import split_html_lines, thread_param

if TYPE_CHECKING:
    from .prefs import Prefs

log = logging.getLogger(__name__)

# Short lines for the logs topic — no details (who/what/where), just the fact of the event.
LOG_LABELS: dict[str, str] = texts.LOG_LABELS


async def notify_event(bot: Bot, settings: Settings, prefs: "Prefs", event: str, text: str) -> None:
    """Detailed notification to admins, honoring the event's toggle in the admin settings."""
    if not await prefs.get_notify(event):
        return
    await notify_admins(bot, settings, text)


async def post_log(bot: Bot, settings: Settings, prefs: "Prefs", event: str) -> None:
    """Short entry in the logs topic (no details), honoring the logs toggle."""
    topic = settings.topic_logs_id
    if topic is None:
        return
    if not await prefs.get_log(event):
        return
    text = LOG_LABELS.get(event)
    if not text:
        return
    try:
        await bot.send_message(
            settings.chat_id,
            text,
            message_thread_id=thread_param(topic),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except TelegramAPIError as exc:
        log.warning(texts.LOG["notify"]["log_topic_failed"], topic, exc)


async def announce(bot: Bot, settings: Settings, prefs: "Prefs", event: str, text: str) -> None:
    """Fire an event to both places: detailed to admins (notify) and short to the logs topic (post_log)."""
    await notify_event(bot, settings, prefs, event, text)
    await post_log(bot, settings, prefs, event)


async def notify_admins(bot: Bot, settings: Settings, text: str) -> None:
    """To the log chat if set, otherwise to each admin in private chat."""
    targets: list[tuple[int, int | None]]
    if settings.admin_log_chat_id:
        targets = [(settings.admin_log_chat_id, settings.admin_log_thread_id)]
    else:
        targets = [(admin_id, None) for admin_id in settings.admin_ids]
    for chat_id, thread_id in targets:
        for chunk in split_html_lines(text):
            try:
                await bot.send_message(
                    chat_id,
                    chunk,
                    message_thread_id=thread_id,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except TelegramAPIError as exc:
                log.warning(texts.LOG["notify"]["admin_failed"], chat_id, exc)
                break
