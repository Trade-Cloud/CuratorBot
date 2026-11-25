"""Author (or admin) editing their own report in private chat:
editing text fields and managing attachments. Entry point — the "Edit" button in the "Details" card."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from .. import keyboards, texts
from ..config import Settings
from ..db.models import Attachment, Report
from ..services import reports
from ..services.llm import Summarizer
from ..services.media import MediaDownloader, attachment_from_ref, extract_files
from ..services.notify import announce
from ..services.prefs import Prefs
from ..states import EditForm
from ..utils.ids import normalize_public_id
from ..utils.telegram import user_html_from
from ..web.events import bus

log = logging.getLogger(__name__)

router = Router(name="edit")
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


def _fields(report: Report) -> tuple[tuple[str, str], ...]:
    return reports.BUG_FIELDS if report.kind == reports.KIND_BUG else reports.FEATURE_FIELDS


async def _load(session: AsyncSession, settings: Settings, rid: str, user_id: int) -> Report | None:
    pid = normalize_public_id(rid)
    report = await reports.get_report(session, pid) if pid else None
    if report is None or not reports.can_view(report, user_id, settings):
        return None
    return report


# ---------------------------------------------------------------- rendering


def _menu(report: Report) -> tuple[str, InlineKeyboardMarkup]:
    lines = [texts.EDIT_MENU.format(rid=report.public_id), ""]
    kb = InlineKeyboardBuilder()
    for key, title in _fields(report):
        value = str(report.fields.get(key) or "")
        preview = (value[:40] + "…") if len(value) > 40 else (value or "—")
        lines.append(texts.EDIT_MENU_ROW.format(title=escape(title), preview=escape(preview)))
        kb.button(text=texts.EDIT_FIELD_BTN.format(title=title), callback_data=keyboards.EditCb(rid=report.public_id, action="field", arg=key))
    kb.button(
        text=texts.BTN_EDIT_ATTACHMENTS.format(n=len(report.attachments)),
        callback_data=keyboards.EditCb(rid=report.public_id, action="att"),
    )
    kb.button(text=texts.BTN_EDIT_DONE, callback_data=keyboards.EditCb(rid=report.public_id, action="done"))
    kb.adjust(1)
    return "\n".join(lines), kb.as_markup()


def _att_menu(report: Report) -> tuple[str, InlineKeyboardMarkup]:
    atts = report.attachments
    lines = [texts.EDIT_ATT_HEADER.format(n=len(atts))]
    kb = InlineKeyboardBuilder()
    if not atts:
        lines.append(texts.EDIT_ATT_EMPTY)
    for a in atts:
        name = a.file_name or a.kind
        kb.button(text=texts.EDIT_DELETE_BTN.format(name=name)[:40], callback_data=keyboards.EditCb(rid=report.public_id, action="delatt", arg=str(a.id)))
    kb.adjust(1)
    kb.button(text=texts.BTN_EDIT_ADD_ATT, callback_data=keyboards.EditCb(rid=report.public_id, action="addatt"))
    kb.button(text=texts.BTN_EDIT_BACK, callback_data=keyboards.EditCb(rid=report.public_id, action="menu"))
    kb.adjust(1)
    return "\n".join(lines), kb.as_markup()


async def _show(callback: CallbackQuery, rendered: tuple[str, InlineKeyboardMarkup]) -> None:
    text, markup = rendered
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except TelegramAPIError:
            await callback.message.answer(text, reply_markup=markup)


async def _touch(
    bot: Bot, session: AsyncSession, report: Report, settings: Settings, summarizer: Summarizer,
    prefs: Prefs, editor_name: str, *, refresh: bool,
) -> None:
    """Record an edit: update the chat post (field edits only), notify the web and admins."""
    report.updated_at = datetime.now(timezone.utc)
    await session.flush()
    if refresh:  # attachments are not shown in the post, so there is no need to rebuild it
        await reports.refresh_post(bot, report, settings, summarizer)
    bus.publish({"type": "report", "id": report.public_id, "kind": "edit"})
    kind = texts.EDIT_KIND_BUG if report.kind == reports.KIND_BUG else texts.EDIT_KIND_FEATURE
    await announce(
        bot, settings, prefs, "report_edited",
        texts.EDIT_ANNOUNCE.format(kind=kind, rid=report.public_id, editor=editor_name),
    )


# ---------------------------------------------------------------- menu entry


@router.callback_query(keyboards.EditCb.filter(F.action == "menu"))
async def open_menu(callback: CallbackQuery, callback_data: keyboards.EditCb, session: AsyncSession, settings: Settings, state: FSMContext) -> None:
    report = await _load(session, settings, callback_data.rid, callback.from_user.id)
    if report is None:
        await callback.answer(texts.EDIT_DENIED, show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await _show(callback, _menu(report))


@router.callback_query(keyboards.EditCb.filter(F.action == "done"))
async def done(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(texts.EDIT_DONE, reply_markup=None)
        except TelegramAPIError:
            pass


# ---------------------------------------------------------------- field editing


@router.callback_query(keyboards.EditCb.filter(F.action == "field"))
async def ask_field(callback: CallbackQuery, callback_data: keyboards.EditCb, session: AsyncSession, settings: Settings, state: FSMContext) -> None:
    report = await _load(session, settings, callback_data.rid, callback.from_user.id)
    if report is None:
        await callback.answer(texts.EDIT_DENIED, show_alert=True)
        return
    field = callback_data.arg
    title = dict(_fields(report)).get(field, field)
    await state.set_state(EditForm.value)
    await state.set_data({"rid": report.public_id, "field": field})
    await callback.answer()
    current = str(report.fields.get(field) or "")
    text = texts.EDIT_ASK_FIELD.format(title=title)
    if current:
        text += "\n\n" + texts.EDIT_FIELD_CURRENT.format(value=escape(current[:500]))
    await _show(callback, (text, InlineKeyboardBuilder().button(
        text=texts.BTN_EDIT_BACK, callback_data=keyboards.EditCb(rid=report.public_id, action="menu")).as_markup()))


@router.message(EditForm.value, F.text, ~F.text.startswith("/"))
async def save_field(
    message: Message, state: FSMContext, bot: Bot, session: AsyncSession, settings: Settings,
    summarizer: Summarizer, prefs: Prefs,
) -> None:
    assert message.from_user
    data = await state.get_data()
    rid, field = data.get("rid", ""), data.get("field", "")
    report = await _load(session, settings, rid, message.from_user.id)
    if report is None:
        await state.clear()
        await message.answer(texts.EDIT_DENIED)
        return
    value = message.text.strip()
    limit = reports.FIELD_LIMITS.get(field, 2000)
    if len(value) > limit:
        await message.answer(texts.TOO_LONG.format(n=len(value), max=limit))
        return
    reports.edit_field(report, field, value)
    await _touch(bot, session, report, settings, summarizer, prefs, user_html_from(message.from_user), refresh=True)
    await state.clear()
    await message.answer(texts.EDIT_SAVED)
    text, markup = _menu(report)
    await message.answer(text, reply_markup=markup)


# ---------------------------------------------------------------- attachments


@router.callback_query(keyboards.EditCb.filter(F.action == "att"))
async def att_menu(callback: CallbackQuery, callback_data: keyboards.EditCb, session: AsyncSession, settings: Settings, state: FSMContext) -> None:
    report = await _load(session, settings, callback_data.rid, callback.from_user.id)
    if report is None:
        await callback.answer(texts.EDIT_DENIED, show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await _show(callback, _att_menu(report))


@router.callback_query(keyboards.EditCb.filter(F.action == "delatt"))
async def del_att(
    callback: CallbackQuery, callback_data: keyboards.EditCb, session: AsyncSession, settings: Settings,
    bot: Bot, summarizer: Summarizer, prefs: Prefs,
) -> None:
    report = await _load(session, settings, callback_data.rid, callback.from_user.id)
    if report is None:
        await callback.answer(texts.EDIT_DENIED, show_alert=True)
        return
    att = await session.get(Attachment, int(callback_data.arg)) if callback_data.arg.isdigit() else None
    if att is not None and att.report_id == report.id:
        if att.local_path:
            try:
                p = Path(att.local_path).resolve()
                if p.is_relative_to(settings.media_dir.resolve()) and p.is_file():
                    p.unlink()
            except OSError:
                pass
        await session.delete(att)
        await session.flush()
        await session.refresh(report, ["attachments"])
        await _touch(bot, session, report, settings, summarizer, prefs, user_html_from(callback.from_user), refresh=False)
        await callback.answer(texts.EDIT_ATT_DELETED)
    else:
        await callback.answer()
    await _show(callback, _att_menu(report))


@router.callback_query(keyboards.EditCb.filter(F.action == "addatt"))
async def add_att(callback: CallbackQuery, callback_data: keyboards.EditCb, session: AsyncSession, settings: Settings, state: FSMContext) -> None:
    report = await _load(session, settings, callback_data.rid, callback.from_user.id)
    if report is None:
        await callback.answer(texts.EDIT_DENIED, show_alert=True)
        return
    await state.set_state(EditForm.attachment)
    await state.set_data({"rid": report.public_id})
    await callback.answer()
    await _show(callback, (texts.EDIT_ATT_ASK, InlineKeyboardBuilder().button(
        text=texts.BTN_EDIT_DONE, callback_data=keyboards.EditCb(rid=report.public_id, action="att")).as_markup()))


@router.message(EditForm.attachment, Command("done"))
async def add_att_done(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    assert message.from_user
    data = await state.get_data()
    report = await _load(session, settings, data.get("rid", ""), message.from_user.id)
    await state.clear()
    if report is not None:
        text, markup = _att_menu(report)
        await message.answer(text, reply_markup=markup)


@router.message(EditForm.attachment, ~F.text.startswith("/"))
async def add_att_file(
    message: Message, state: FSMContext, session: AsyncSession, settings: Settings,
    media: MediaDownloader, bot: Bot, summarizer: Summarizer, prefs: Prefs,
) -> None:
    assert message.from_user
    files = extract_files(message)
    if not files:
        await message.answer(texts.EDIT_ATT_ASK)
        return
    data = await state.get_data()
    report = await _load(session, settings, data.get("rid", ""), message.from_user.id)
    if report is None:
        await state.clear()
        await message.answer(texts.EDIT_DENIED)
        return
    rows = [attachment_from_ref(f, report_id=report.id) for f in files]
    session.add_all(rows)
    await session.flush()
    att_ids = [r.id for r in rows]
    media.schedule(att_ids, subdir=f"reports/{report.public_id}")
    await session.refresh(report, ["attachments"])
    group = message.media_group_id
    should_reply = group is None or data.get("last_group") != group
    await state.update_data(last_group=group)
    if should_reply:  # for an album, record the edit and reply once
        await _touch(bot, session, report, settings, summarizer, prefs, user_html_from(message.from_user), refresh=False)
        await message.answer(
            texts.EDIT_ATT_ADDED.format(n=len(report.attachments)),
            reply_markup=InlineKeyboardBuilder().button(
                text=texts.BTN_EDIT_DONE, callback_data=keyboards.EditCb(rid=report.public_id, action="att")).as_markup(),
        )
