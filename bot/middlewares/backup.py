from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update
from sqlalchemy.ext.asyncio import async_sessionmaker

from .. import texts
from ..services import backup
from ..services.media import MediaDownloader
from ..web.events import bus

log = logging.getLogger(__name__)


class BackupMiddleware(BaseMiddleware):
    """Update outer-middleware: saves everything from the beta chat BEFORE handlers, in its own transaction,
    so the backup is not rolled back if a handler fails (and so moderation sees an already-saved message)."""

    def __init__(self, chat_id: int, sessionmaker: async_sessionmaker, media: MediaDownloader):
        self.chat_id = chat_id
        self.sessionmaker = sessionmaker
        self.media = media

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Update) and backup.update_chat_id(event) == self.chat_id:
            try:
                await self._save(event)
            except Exception:
                # The backup must never break update processing.
                log.exception(texts.LOG["backup"]["failed"], event.update_id)
        return await handler(event, data)

    async def _save(self, update: Update) -> None:
        att_ids: list[int] = []
        subdir = "chat/misc"
        event: dict | None = None
        async with self.sessionmaker() as session:
            await backup.save_raw_update(session, update)
            message = update.message or update.edited_message
            if message is not None:
                subdir = f"chat/{message.date:%Y-%m}"
                if message.from_user:
                    await backup.upsert_user(session, message.from_user, message.sender_tag)
                event = {
                    "type": "message" if update.message else "edit",
                    "chat_id": message.chat.id,
                    "message_id": message.message_id,
                }
                if update.message:
                    att_ids = await backup.save_message(session, message)
                    await backup.save_topic_event(session, message)
                else:
                    att_ids, version_id = await backup.save_edit(session, message)
                    event["version_id"] = version_id
            elif update.chat_member and update.chat_member.new_chat_member:
                await backup.upsert_user(session, update.chat_member.new_chat_member.user)
            await session.commit()
        if event:
            bus.publish(event)
        if att_ids:
            self.media.schedule(att_ids, subdir=subdir)
