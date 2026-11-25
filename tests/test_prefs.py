"""Runtime settings: notifications and form quick buttons (in-memory SQLite)."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db.models import Base
from bot.services.prefs import Prefs


@pytest.fixture
async def sm():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_notify_defaults_and_set(sm):
    p = Prefs(sm)
    assert await p.get_notify("report_bug") is True  # on by default
    await p.set_notify("report_bug", False)
    assert await p.get_notify("report_bug") is False
    assert (await p.all_notify())["report_bug"] is False
    # Persisted to the DB — a new instance sees the same.
    assert await Prefs(sm).get_notify("report_bug") is False


async def test_notify_toggle(sm):
    p = Prefs(sm)
    assert await p.toggle_notify("member_join") is False
    assert await p.toggle_notify("member_join") is True


async def test_logs_toggles_independent_of_notify(sm):
    p = Prefs(sm)
    assert await p.get_log("report_bug") is True  # on by default
    await p.set_log("report_bug", False)
    assert await p.get_log("report_bug") is False
    # Logs and notifications are independent sets.
    assert await p.get_notify("report_bug") is True
    assert await p.toggle_log("member_join") is False
    assert await Prefs(sm).get_log("report_bug") is False  # persisted to the DB


async def test_buttons_default_add_remove(sm):
    p = Prefs(sm)
    assert [b["value"] for b in await p.list_buttons("environment")] == ["Browser"]
    await p.add_button("environment", "🖥 Postman", "Postman")
    assert [b["value"] for b in await p.list_buttons("environment")] == ["Browser", "Postman"]
    await p.remove_button("environment", 0)  # remove the default "Browser"
    assert [b["value"] for b in await p.list_buttons("environment")] == ["Postman"]
    # Removing the default survives a restart (an explicit list is stored).
    assert [b["value"] for b in await Prefs(sm).list_buttons("environment")] == ["Postman"]
    # A step with no configured buttons is empty.
    assert await p.list_buttons("steps") == []
