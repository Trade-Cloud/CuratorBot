"""Short logs to the topic: post_log honors the topic and the toggle."""

from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db.models import Base
from bot.services import notify as notify_mod
from bot.services.prefs import Prefs


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw.get("message_thread_id")))


@pytest.fixture
async def sm():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _settings(topic_logs_id):
    return SimpleNamespace(chat_id=-100, topic_logs_id=topic_logs_id)


async def test_post_log_posts_terse_line(sm):
    bot = FakeBot()
    await notify_mod.post_log(bot, _settings(7), Prefs(sm), "report_bug")
    assert len(bot.sent) == 1
    chat_id, text, thread = bot.sent[0]
    assert chat_id == -100 and thread == 7 and "bug" in text.lower()


async def test_post_log_skipped_without_topic(sm):
    bot = FakeBot()
    await notify_mod.post_log(bot, _settings(None), Prefs(sm), "report_bug")
    assert bot.sent == []


async def test_post_log_skipped_when_toggle_off(sm):
    bot, prefs = FakeBot(), Prefs(sm)
    await prefs.set_log("report_bug", False)
    await notify_mod.post_log(bot, _settings(7), prefs, "report_bug")
    assert bot.sent == []
