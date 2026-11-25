"""Report editor: field editing and menu rendering."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot import handlers  # noqa: F401  (registers routers, warms up the decorators)
from bot.db.models import Attachment, Base, Report
from bot.handlers import edit as edit_mod
from bot.handlers.edit import _att_menu, _menu
from bot.services import reports
from bot.services.prefs import Prefs
from bot.states import EditForm


def _report(kind="bug", fields=None):
    r = Report(public_id="ABCDEF", kind=kind, status="open", user_id=1, fields=fields or {"description": "d"})
    r.attachments = []
    return r


def _datas(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_edit_field_clears_bug_summary():
    r = _report("bug", {"description": "old", "summary": "summary"})
    reports.edit_field(r, "description", "new")
    assert r.fields["description"] == "new"
    assert "summary" not in r.fields  # the post recomputes the summary


def test_edit_field_keeps_feature_value():
    r = _report("feature", {"idea": "old"})
    reports.edit_field(r, "idea", "new")
    assert r.fields["idea"] == "new"


def test_menu_lists_fields_attachments_done():
    datas = _datas(_menu(_report("bug", {"description": "d"}))[1])
    assert "edit:ABCDEF:field:description" in datas
    assert "edit:ABCDEF:field:environment" in datas  # all bug fields
    assert any(d.startswith("edit:ABCDEF:att") for d in datas)
    assert any(d.startswith("edit:ABCDEF:done") for d in datas)


def test_feature_menu_uses_feature_fields():
    datas = _datas(_menu(_report("feature", {"idea": "i"}))[1])
    assert "edit:ABCDEF:field:idea" in datas
    assert "edit:ABCDEF:field:implementation" in datas
    assert not any(":field:description" in d for d in datas)  # a feature has no bug fields


def test_att_menu_delete_add_back():
    r = _report("feature", {"idea": "i"})
    att = Attachment(kind="photo", file_id="f", file_unique_id="u", file_name="a.png", status="done")
    att.id = 7
    r.attachments = [att]
    datas = _datas(_att_menu(r)[1])
    assert "edit:ABCDEF:delatt:7" in datas
    assert any(d.startswith("edit:ABCDEF:addatt") for d in datas)
    assert any(d.startswith("edit:ABCDEF:menu") for d in datas)


# ------------------------------------------------------------ end-to-end run of the field-edit handler

UID = 555
CHAT = -100777


class _Msg:
    def __init__(self, uid, text):
        self.from_user = SimpleNamespace(id=uid, username="u", first_name="U", last_name=None)
        self.text = text
        self.answers = []

    async def answer(self, text, reply_markup=None):
        self.answers.append(text)


class _Bot:
    def __init__(self):
        self.edits = []

    async def edit_message_text(self, **kw):
        self.edits.append(kw)

    async def send_message(self, *a, **kw):
        pass


class _Sum:
    async def summarize_bug(self, fields, limit):
        return "fresh summary"


def _settings():
    return SimpleNamespace(
        is_admin=lambda u: False, chat_id=CHAT, timezone="UTC", bug_summary_chars=200,
        media_dir=Path("/tmp/nope"), admin_log_chat_id=None, admin_log_thread_id=None, admin_ids=set(),
        topic_logs_id=None,
    )


@pytest.fixture
async def sm():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_save_field_updates_report_and_post(sm):
    async with sm() as s:
        report, _ = await reports.create_report(
            s, kind="bug", user=SimpleNamespace(id=UID, username="u", first_name="U", last_name=None),
            fields={"description": "old", "summary": "old summary"}, started_at=None,
        )
        report.post_chat_id, report.post_message_id, report.post_thread_id = CHAT, 99, 4
        await s.commit()
        rid = report.public_id

    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=UID, user_id=UID))
    await state.set_state(EditForm.value)
    await state.set_data({"rid": rid, "field": "description"})

    bot, msg = _Bot(), _Msg(UID, "new description")
    async with sm() as s:
        await edit_mod.save_field(msg, state, bot, s, _settings(), _Sum(), Prefs(sm))
        await s.commit()

    # The bug post shows the AI summary — it was recomputed from the new fields.
    assert bot.edits and "fresh summary" in bot.edits[0]["text"]
    assert await state.get_state() is None
    assert any("Saved" in a for a in msg.answers)

    async with sm() as s:
        fresh = await reports.get_report(s, rid)
        assert fresh.fields["description"] == "new description"
        assert fresh.fields["summary"] == "fresh summary"  # summary recomputed


async def test_save_field_rejects_non_author(sm):
    async with sm() as s:
        report, _ = await reports.create_report(
            s, kind="feature", user=SimpleNamespace(id=UID, username="u", first_name="U", last_name=None),
            fields={"idea": "idea"}, started_at=None,
        )
        await s.commit()
        rid = report.public_id

    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=999, user_id=999))
    await state.set_state(EditForm.value)
    await state.set_data({"rid": rid, "field": "idea"})

    msg = _Msg(999, "foreign edit")  # not the author and not an admin
    async with sm() as s:
        await edit_mod.save_field(msg, state, _Bot(), s, _settings(), _Sum(), Prefs(sm))
        await s.commit()

    async with sm() as s:
        fresh = await reports.get_report(s, rid)
        assert fresh.fields["idea"] == "idea"  # unchanged
