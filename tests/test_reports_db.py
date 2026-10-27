"""Создание репорта и уникальность ID на SQLite в памяти."""

from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db.models import Base
from bot.services import reports
from bot.services.media import FileRef


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


def _user(uid=1):
    return SimpleNamespace(id=uid, username="tester", first_name="Test", last_name=None)


async def test_create_bug_with_files(session):
    report, att_ids = await reports.create_report(
        session,
        kind=reports.KIND_BUG,
        user=_user(),
        fields={"description": "Падает логин", "environment": "Браузер"},
        started_at=None,
        files=[FileRef("photo", "fid", "uid1"), FileRef("document", "fid2", "uid2", "log.txt")],
    )
    await session.commit()
    assert len(report.public_id) == 6
    assert len(att_ids) == 2
    loaded = await reports.get_report(session, report.public_id)
    assert loaded is not None and len(loaded.attachments) == 2


async def test_public_ids_unique(session):
    ids = set()
    for _ in range(30):
        r, _ = await reports.create_report(
            session, kind=reports.KIND_FEATURE, user=_user(), fields={"idea": "x"}, started_at=None
        )
        ids.add(r.public_id)
    assert len(ids) == 30


def test_can_view():
    settings = SimpleNamespace(is_admin=lambda uid: uid == 42)
    report = SimpleNamespace(user_id=7)
    assert reports.can_view(report, 7, settings)
    assert reports.can_view(report, 42, settings)
    assert not reports.can_view(report, 8, settings)
