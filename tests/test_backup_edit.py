"""Edits: text version, media replacement, the tiny column migration."""

from datetime import datetime, timezone

import pytest
from aiogram.types import Message
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db import init_db
from bot.db.models import Attachment, MessageVersion
from bot.services import backup

CHAT = {"id": -1001, "type": "supergroup", "title": "beta", "is_forum": True}
USER = {"id": 10, "is_bot": False, "first_name": "Vasya"}
TS = int(datetime(2026, 10, 27, tzinfo=timezone.utc).timestamp())


def photo(uid: str) -> list[dict]:
    return [{"file_id": f"f-{uid}", "file_unique_id": uid, "width": 100, "height": 100}]


def msg(**kw) -> Message:
    data = {"message_id": 5, "date": TS, "chat": CHAT, "from": USER, **kw}
    return Message.model_validate(data)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def test_text_edit_creates_version(session):
    await backup.save_message(session, msg(text="old"))
    _, version_id = await backup.save_edit(session, msg(text="new", edit_date=TS + 60))
    v = await session.get(MessageVersion, version_id)
    assert (v.old_text, v.new_text) == ("old", "new")


async def test_photo_replacement(session):
    await backup.save_message(session, msg(photo=photo("A"), caption="screenshot"))
    new_att_ids, version_id = await backup.save_edit(session, msg(photo=photo("B"), caption="screenshot", edit_date=TS + 5))
    assert len(new_att_ids) == 1
    atts = {a.file_unique_id: a for a in (await session.execute(select(Attachment))).scalars()}
    assert atts["A"].replaced_by_edit is True
    assert not atts["B"].replaced_by_edit
    v = await session.get(MessageVersion, version_id)
    assert v.old_attachments == [atts["A"].id] and v.new_attachments == [atts["B"].id]


async def test_caption_only_edit_keeps_media(session):
    await backup.save_message(session, msg(photo=photo("A"), caption="one"))
    new_att_ids, version_id = await backup.save_edit(session, msg(photo=photo("A"), caption="two", edit_date=TS + 5))
    assert new_att_ids == []
    v = await session.get(MessageVersion, version_id)
    assert v.old_attachments == v.new_attachments


async def test_missing_columns_are_added():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE attachments (id INTEGER PRIMARY KEY, kind VARCHAR(32))"))
    await init_db(engine)
    async with engine.connect() as conn:
        cols = {row[1] for row in (await conn.execute(text("PRAGMA table_info(attachments)"))).all()}
    assert "replaced_by_edit" in cols
    await engine.dispose()
