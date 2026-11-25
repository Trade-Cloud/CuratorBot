"""Web viewer API on in-memory SQLite."""

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
        topic_main_id=0, topic_logs_id=None, web_password=None, web_user="admin",
        media_dir=__import__("pathlib").Path("/tmp/nope"),
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
        s.add(ChatUser(id=10, username="vasya", first_name="Vasya", last_tag="MODER"))
        s.add_all([
            ChatMessage(chat_id=CHAT, message_id=1, thread_id=0, user_id=10, date=now, content_type="text",
                        text="hello", raw={}),
            ChatMessage(chat_id=CHAT, message_id=2, thread_id=3, user_id=10, date=now, content_type="photo",
                        text="screenshot", reply_to_message_id=1, raw={}, is_edited=True, edit_count=1),
        ])
        await s.flush()
        s.add(MessageVersion(message_pk=2, version=1, old_text="screnshot", new_text="screenshot", raw={}))
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
        assert [m["text"] for m in msgs] == ["hello", "screenshot"]
        photo = msgs[1]
        assert photo["user"]["tag"] == "MODER"
        assert photo["reply"]["text"] == "hello"
        assert photo["attachments"][0]["url"] is None  # not downloaded yet

        only_bugs = await (await client.get("/api/messages?thread=3")).json()
        assert len(only_bugs) == 1
        found = await (await client.get("/api/messages?q=hel")).json()
        assert len(found) == 1

        versions = await (await client.get(f"/api/messages/{photo['pk']}/versions")).json()
        assert versions[0]["old"] == "screnshot"
        edits = await (await client.get("/api/edits")).json()
        assert edits[0]["new"] == "screenshot" and edits[0]["user"]["username"] == "vasya"
        assert photo["last_change"]["old_text"] == "screnshot"
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


class _FakeBot:
    def __init__(self):
        self.calls = []

    async def edit_message_text(self, **kw):
        self.calls.append(("edit", kw))

    async def send_message(self, *a, **kw):
        self.calls.append(("dm", a))

    async def ban_chat_member(self, chat_id, user_id):
        self.calls.append(("ban", chat_id, user_id))

    async def unban_chat_member(self, chat_id, user_id, only_if_banned=None):
        self.calls.append(("unban", chat_id, user_id))

    async def restrict_chat_member(self, chat_id, user_id, permissions=None, until_date=None):
        self.calls.append(("restrict", chat_id, user_id))


class _FakeSummarizer:
    async def summarize_bug(self, fields, limit):
        return "summary"


async def test_management_endpoints(sessionmaker):
    from bot.services import reports

    async with sessionmaker() as s:
        report, _ = await reports.create_report(
            s, kind="bug", user=SimpleNamespace(id=10, username="vasya", first_name="Vasya", last_name=None),
            fields={"description": "crashes"}, started_at=None,
        )
        await s.commit()
        rid = report.public_id

    bot = _FakeBot()
    app = build_app(
        _settings(), sessionmaker, bot=bot, summarizer=_FakeSummarizer(), bot_username="mybot"
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        meta = await (await client.get("/api/meta")).json()
        assert meta["can_manage"] is True and meta["bot_username"] == "mybot" and "fixed" in meta["statuses"]

        resp = await client.post(f"/api/reports/{rid}/status", json={"status": "fixed", "comment": "done"})
        assert resp.status == 200
        row = [x for x in await (await client.get("/api/reports")).json() if x["id"] == rid][0]
        assert row["status"] == "fixed" and row["status_comment"] == "done"
        assert row["bot_link"].endswith(f"start=r_{rid}")
        assert any(c[0] == "dm" for c in bot.calls)  # the author was notified

        bad = await client.post(f"/api/reports/{rid}/status", json={"status": "no-such"})
        assert bad.status == 400

        drafts = await (await client.get("/api/drafts")).json()
        assert drafts == []

        act = await client.post("/api/members/10/action", json={"action": "ban"})
        assert act.status == 200 and (await act.json())["result"] == "banned"
        assert ("ban", _settings().chat_id, 10) in bot.calls

        assert (await client.post("/api/members/10/action", json={"action": "no"})).status == 400
    finally:
        await client.close()


async def test_management_needs_bot(sessionmaker):
    # Without a bot the control endpoints are unavailable (503), while reading still works.
    client = await _client(sessionmaker, _settings())
    try:
        meta = await (await client.get("/api/meta")).json()
        assert meta["can_manage"] is False
        assert (await client.post("/api/members/10/action", json={"action": "ban"})).status == 503
    finally:
        await client.close()
