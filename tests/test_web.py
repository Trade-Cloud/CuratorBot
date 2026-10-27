"""API веб-просмотрщика на SQLite в памяти."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db.models import Attachment, Base, ChatMessage, ChatUser, MessageVersion
from bot.web.server import build_app

CHAT = -1001


def _settings(**kw):
    base = dict(
        chat_id=CHAT, topic_info_id=2, topic_changelog_id=None, topic_bugs_id=3, topic_features_id=None,
        topic_main_id=0, web_password=None, web_user="admin", media_dir=__import__("pathlib").Path("/tmp/nope"),
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
async def sessionmaker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    async with sm() as s:
        s.add(ChatUser(id=10, username="vasya", first_name="Вася", last_tag="MODER"))
        s.add_all([
            ChatMessage(chat_id=CHAT, message_id=1, thread_id=0, user_id=10, date=now, content_type="text",
                        text="привет", raw={}),
            ChatMessage(chat_id=CHAT, message_id=2, thread_id=3, user_id=10, date=now, content_type="photo",
                        text="скрин", reply_to_message_id=1, raw={}, is_edited=True, edit_count=1),
        ])
        await s.flush()
        s.add(MessageVersion(message_pk=2, version=1, old_text="скрн", new_text="скрин", raw={}))
        s.add(Attachment(message_pk=2, kind="photo", file_id="f", file_unique_id="u", status="pending"))
        await s.commit()
    yield sm
    await engine.dispose()


async def _client(sm, settings):
    client = TestClient(TestServer(build_app(settings, sm)))
    await client.start_server()
    return client


async def test_topics_and_messages(sessionmaker):
    client = await _client(sessionmaker, _settings())
    try:
        topics = await (await client.get("/api/topics")).json()
        by_id = {t["id"]: t for t in topics}
        assert by_id[0]["count"] == 1 and by_id[3]["name"] == "Bug Reports" and by_id[3]["count"] == 1

        msgs = await (await client.get("/api/messages")).json()
        assert [m["text"] for m in msgs] == ["привет", "скрин"]
        photo = msgs[1]
        assert photo["user"]["tag"] == "MODER"
        assert photo["reply"]["text"] == "привет"
        assert photo["attachments"][0]["url"] is None  # ещё не скачан

        only_bugs = await (await client.get("/api/messages?thread=3")).json()
        assert len(only_bugs) == 1
        found = await (await client.get("/api/messages?q=прив")).json()
        assert len(found) == 1

        versions = await (await client.get(f"/api/messages/{photo['pk']}/versions")).json()
        assert versions[0]["old"] == "скрн"
        edits = await (await client.get("/api/edits")).json()
        assert edits[0]["new"] == "скрин" and edits[0]["user"]["username"] == "vasya"
        assert photo["last_change"]["old_text"] == "скрн"
        assert (await client.get("/")).status == 200
    finally:
        await client.close()


async def test_basic_auth(sessionmaker):
    from pydantic import SecretStr

    client = await _client(sessionmaker, _settings(web_password=SecretStr("s3cret")))
    try:
        assert (await client.get("/api/topics")).status == 401
        import aiohttp

        ok = await client.get("/api/topics", auth=aiohttp.BasicAuth("admin", "s3cret"))
        assert ok.status == 200
    finally:
        await client.close()
