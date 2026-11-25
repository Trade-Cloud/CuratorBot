"""Form drafts: lifecycle (in-memory SQLite)."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db.models import Base
from bot.services import drafts


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


def _user(uid=5):
    return SimpleNamespace(id=uid, username="u", first_name="U", last_name=None)


async def test_lifecycle(session):
    u = _user()
    now = datetime.now(timezone.utc)
    assert await drafts.start(session, u, "bug", now) is True  # a new draft
    await drafts.update(session, u.id, fields={"description": "x"}, files_count=2, step="BugForm:steps")

    active = await drafts.list_active(session)
    assert len(active) == 1
    assert active[0].fields["description"] == "x" and active[0].files_count == 2 and active[0].step == "BugForm:steps"

    # Restarting while a draft is active is no longer "new" (no repeat notification).
    assert await drafts.start(session, u, "bug", now) is False

    await drafts.mark_submitted(session, u.id)
    assert await drafts.list_active(session) == []

    # After finishing, a new start counts as new again.
    assert await drafts.start(session, u, "feature", now) is True
    await drafts.mark_cancelled(session, u.id)
    assert await drafts.list_active(session) == []


async def test_update_ignores_finished(session):
    u = _user(7)
    await drafts.start(session, u, "bug", None)
    await drafts.mark_cancelled(session, u.id)
    # update after cancellation touches nothing and does not revive the draft.
    await drafts.update(session, u.id, fields={"description": "y"})
    assert await drafts.list_active(session) == []
