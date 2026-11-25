"""Extract files from messages and save copies to disk in the background."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message
from sqlalchemy.ext.asyncio import async_sessionmaker

from .. import texts
from ..db.models import Attachment
from ..utils.telegram import spawn
from ..web.events import bus

log = logging.getLogger(__name__)

# Order only matters for the logs; a single message usually has one file.
_FILE_KINDS = (
    "document",
    "video",
    "animation",
    "audio",
    "voice",
    "video_note",
    "sticker",
)
_SAFE_NAME = re.compile(r"[^\w.\-]+")


@dataclass(slots=True)
class FileRef:
    kind: str
    file_id: str
    file_unique_id: str
    file_name: str | None = None
    mime_type: str | None = None
    file_size: int | None = None


def extract_files(message: Message) -> list[FileRef]:
    files: list[FileRef] = []
    if message.photo:
        best = max(message.photo, key=lambda p: (p.width * p.height, p.file_size or 0))
        files.append(FileRef("photo", best.file_id, best.file_unique_id, None, "image/jpeg", best.file_size))
    for kind in _FILE_KINDS:
        obj = getattr(message, kind, None)
        if obj is None:
            continue
        files.append(
            FileRef(
                kind=kind,
                file_id=obj.file_id,
                file_unique_id=obj.file_unique_id,
                file_name=getattr(obj, "file_name", None),
                mime_type=getattr(obj, "mime_type", None),
                file_size=getattr(obj, "file_size", None),
            )
        )
    return files


def attachment_from_ref(ref: FileRef, **owner: int | None) -> Attachment:
    return Attachment(
        kind=ref.kind,
        file_id=ref.file_id,
        file_unique_id=ref.file_unique_id,
        file_name=ref.file_name,
        mime_type=ref.mime_type,
        file_size=ref.file_size,
        status="pending",
        **owner,
    )


def _target_path(root: Path, subdir: str, att: Attachment) -> Path:
    ext = ""
    if att.file_name and "." in att.file_name:
        ext = "." + att.file_name.rsplit(".", 1)[1]
    elif att.mime_type:
        ext = mimetypes.guess_extension(att.mime_type) or ""
    elif att.kind == "photo":
        ext = ".jpg"
    base = _SAFE_NAME.sub("_", att.file_name.rsplit(".", 1)[0]) if att.file_name else att.kind
    return root / subdir / f"{att.id}_{att.file_unique_id}_{base[:80]}{ext}"


class MediaDownloader:
    def __init__(self, bot: Bot, sessionmaker: async_sessionmaker, root: Path, concurrency: int, max_bytes: int):
        self.bot = bot
        self.sessionmaker = sessionmaker
        self.root = root
        self.max_bytes = max_bytes
        self._sem = asyncio.Semaphore(concurrency)

    def schedule(self, attachment_ids: list[int], subdir: str) -> None:
        for att_id in attachment_ids:
            spawn(self._download(att_id, subdir))

    async def _download(self, att_id: int, subdir: str) -> None:
        async with self._sem:
            async with self.sessionmaker() as session:
                att = await session.get(Attachment, att_id)
                if att is None or att.status == "done":
                    return
                if att.file_size and att.file_size > self.max_bytes:
                    att.status = "too_big"
                    await session.commit()
                    return
                path = _target_path(self.root, subdir, att)
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    await self.bot.download(att.file_id, destination=path)
                except TelegramAPIError as exc:
                    att.status = "too_big" if "too big" in str(exc).lower() else "failed"
                    att.error = str(exc)[:1000]
                except OSError as exc:
                    att.status = "failed"
                    att.error = str(exc)[:1000]
                else:
                    att.status = "done"
                    att.local_path = str(path)
                await session.commit()
                if att.message_pk:
                    bus.publish({"type": "attachment", "message_pk": att.message_pk, "status": att.status})
                if att.status != "done":
                    log.warning(texts.LOG["media"]["not_downloaded"], att_id, att.status, att.error)
