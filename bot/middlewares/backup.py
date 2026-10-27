from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..services import backup
from ..services.media import MediaDownloader

log = logging.getLogger(__name__)


class BackupMiddleware(BaseMiddleware):
    """Outer-middleware на Update: сохраняет всё из бета-чата ДО хендлеров и в своей транзакции,
    чтобы бекап не откатился, если хендлер упадёт (и чтобы модерация видела уже сохранённое сообщение)."""

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
                # Бекап не должен ронять обработку апдейта.
                log.exception("backup failed for update %s", event.update_id)
        return await handler(event, data)

    async def _save(self, update: Update) -> None:
        att_ids: list[int] = []
        subdir = "chat/misc"
        async with self.sessionmaker() as session:
            await backup.save_raw_update(session, update)
            message = update.message or update.edited_message
            if message is not None:
                subdir = f"chat/{message.date:%Y-%m}"
                if message.from_user:
                    await backup.upsert_user(session, message.from_user, message.sender_tag)
                if update.message:
                    att_ids = await backup.save_message(session, message)
                    await backup.save_topic_event(session, message)
                else:
                    att_ids = await backup.save_edit(session, message)
            elif update.chat_member and update.chat_member.new_chat_member:
                await backup.upsert_user(session, update.chat_member.new_chat_member.user)
            await session.commit()
        if att_ids:
            self.media.schedule(att_ids, subdir=subdir)
