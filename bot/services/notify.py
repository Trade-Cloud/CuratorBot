from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import LinkPreviewOptions

from ..config import Settings
from ..utils.telegram import split_html_lines

log = logging.getLogger(__name__)


async def notify_admins(bot: Bot, settings: Settings, text: str) -> None:
    """В лог-чат, если он задан, иначе каждому админу в ЛС."""
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
                log.warning("notify %s failed: %s", chat_id, exc)
                break
