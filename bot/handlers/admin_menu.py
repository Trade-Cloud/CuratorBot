"""Admin menu in private chat (/admin, ADMIN_IDS only): form quick buttons, connected people,
drafts, notification settings, member actions."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import texts
from ..config import CONFIG, Settings
from ..db.models import ChatMessage, ChatUser
from ..services import drafts, members
from ..services.member_tags import MemberTagResolver
from ..services.prefs import BUTTON_STEPS, NOTIFY_EVENTS, Prefs
from ..states import AdminButtonForm
from ..utils.telegram import user_html

log = logging.getLogger(__name__)

PAGE = CONFIG["pagination"]["admin_page_size"]
FIELD_LABELS = texts.FIELD_LABELS


class AdminCb(CallbackData, prefix="adm"):
    section: str  # home | buttons | notify | people | drafts | member
    action: str = "open"
    arg: str = ""


def _since(dt: datetime | None) -> str:
    if dt is None:
        return texts.SINCE_NONE
    seconds = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return texts.SINCE_HM.format(h=h, m=m)
    if m:
        return texts.SINCE_M.format(m=m)
    return texts.SINCE_S.format(s=s)


# ---------------------------------------------------------------- screen rendering


def _home() -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.ADMIN_HOME_BUTTONS, callback_data=AdminCb(section="buttons", action="list"))
    kb.button(text=texts.ADMIN_HOME_PEOPLE, callback_data=AdminCb(section="people", action="list", arg="0"))
    kb.button(text=texts.ADMIN_HOME_DRAFTS, callback_data=AdminCb(section="drafts", action="list", arg="0"))
    kb.button(text=texts.ADMIN_HOME_NOTIFY, callback_data=AdminCb(section="notify", action="list"))
    kb.button(text=texts.ADMIN_HOME_LOGS, callback_data=AdminCb(section="logs", action="list"))
    kb.adjust(1)
    return texts.ADMIN_HOME, kb.as_markup()


def _back_button(kb: InlineKeyboardBuilder, section: str, action: str = "open", arg: str = "") -> None:
    kb.button(text=texts.ADMIN_BACK, callback_data=AdminCb(section=section, action=action, arg=arg))


async def _buttons_list() -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardBuilder()
    for step, label in BUTTON_STEPS.items():
        kb.button(text=label, callback_data=AdminCb(section="buttons", action="step", arg=step))
    kb.adjust(1)
    _back_button(kb, "home")
    return texts.ADMIN_BUTTONS_LIST, kb.as_markup()


async def _buttons_step(prefs: Prefs, step: str) -> tuple[str, InlineKeyboardMarkup]:
    buttons = await prefs.list_buttons(step)
    kb = InlineKeyboardBuilder()
    lines = [f"<b>{escape(BUTTON_STEPS.get(step, step))}</b>", ""]
    if buttons:
        for i, b in enumerate(buttons):
            lines.append(
                texts.ADMIN_BUTTONS_ROW.format(
                    i=i + 1, label=escape(b.get("label", "")), value=escape(b.get("value", ""))
                )
            )
            kb.button(
                text=texts.ADMIN_BUTTONS_DELETE.format(label=b.get("label", "")[:24]),
                callback_data=AdminCb(section="buttons", action="del", arg=f"{step}:{i}"),
            )
    else:
        lines.append(texts.ADMIN_BUTTONS_EMPTY)
    kb.adjust(1)
    kb.button(text=texts.ADMIN_BUTTONS_ADD, callback_data=AdminCb(section="buttons", action="add", arg=step))
    _back_button(kb, "buttons", "list")
    kb.adjust(1)
    return "\n".join(lines), kb.as_markup()


async def _notify_list(prefs: Prefs) -> tuple[str, InlineKeyboardMarkup]:
    current = await prefs.all_notify()
    kb = InlineKeyboardBuilder()
    for event, label in NOTIFY_EVENTS.items():
        on = current.get(event, True)
        kb.button(text=f"{'✅' if on else '⬜️'} {label}", callback_data=AdminCb(section="notify", action="toggle", arg=event))
    kb.adjust(1)
    _back_button(kb, "home")
    return texts.ADMIN_NOTIFY_LIST, kb.as_markup()


async def _logs_list(prefs: Prefs) -> tuple[str, InlineKeyboardMarkup]:
    current = await prefs.all_logs()
    kb = InlineKeyboardBuilder()
    for event, label in NOTIFY_EVENTS.items():
        on = current.get(event, True)
        kb.button(text=f"{'✅' if on else '⬜️'} {label}", callback_data=AdminCb(section="logs", action="toggle", arg=event))
    kb.adjust(1)
    _back_button(kb, "home")
    return texts.ADMIN_LOGS_LIST, kb.as_markup()


async def _people_list(session: AsyncSession, page: int) -> tuple[str, InlineKeyboardMarkup]:
    total = await session.scalar(select(func.count(ChatUser.id))) or 0
    rows = list(
        (
            await session.execute(
                select(ChatUser).order_by(ChatUser.last_seen.desc()).limit(PAGE).offset(page * PAGE)
            )
        ).scalars()
    )
    kb = InlineKeyboardBuilder()
    for u in rows:
        name = " ".join(p for p in (u.first_name, u.last_name) if p) or str(u.id)
        mark = texts.ADMIN_PEOPLE_ROW_STARTED if u.started_bot else texts.ADMIN_PEOPLE_ROW_PLAIN
        kb.button(text=f"{mark} {name}"[:40], callback_data=AdminCb(section="member", action="open", arg=str(u.id)))
    kb.adjust(1)
    _pager(kb, "people", page, total)
    _back_button(kb, "home")
    kb.adjust(1)
    return texts.ADMIN_PEOPLE_LIST.format(total=total), kb.as_markup()


def _pager(kb: InlineKeyboardBuilder, section: str, page: int, total: int) -> None:
    last = (total - 1) // PAGE if total else 0
    if page > 0:
        kb.button(text="◀️", callback_data=AdminCb(section=section, action="list", arg=str(page - 1)))
    if page < last:
        kb.button(text="▶️", callback_data=AdminCb(section=section, action="list", arg=str(page + 1)))


async def _member_card(
    session: AsyncSession, bot: Bot, settings: Settings, tags: MemberTagResolver, user_id: int, confirm: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    u = await session.get(ChatUser, user_id)
    name = (" ".join(p for p in (u.first_name, u.last_name) if p) if u else None) or str(user_id)
    username = u.username if u else None
    count = await session.scalar(
        select(func.count(ChatMessage.id)).where(ChatMessage.chat_id == settings.chat_id, ChatMessage.user_id == user_id)
    )
    try:
        member = await tags.get(bot, settings.chat_id, user_id)
        status = member.status if member else "?"
    except Exception:  # noqa: BLE001
        status = "?"
    lines = [
        texts.ADMIN_MEMBER_NAME.format(user=user_html(user_id, name, username)),
        texts.ADMIN_MEMBER_STATUS.format(user_id=user_id, status=status),
        texts.ADMIN_MEMBER_MESSAGES.format(n=count or 0),
    ]
    kb = InlineKeyboardBuilder()
    if confirm in members.DESTRUCTIVE:
        lines.append(texts.ADMIN_MEMBER_CONFIRM.format(action=confirm, name=escape(name)))
        kb.button(
            text=texts.ADMIN_MEMBER_CONFIRM_YES.format(label=members.ACTION_LABELS[confirm]),
            callback_data=AdminCb(section="member", action="do", arg=f"{confirm}:{user_id}"),
        )
        kb.button(text=texts.ADMIN_MEMBER_CANCEL, callback_data=AdminCb(section="member", action="open", arg=str(user_id)))
        kb.adjust(1)
        return "\n".join(lines), kb.as_markup()
    kb.button(text=texts.ADMIN_MEMBER_MUTE, callback_data=AdminCb(section="member", action="do", arg=f"mute:{user_id}:60"))
    kb.button(text=texts.ADMIN_MEMBER_UNMUTE, callback_data=AdminCb(section="member", action="do", arg=f"unmute:{user_id}"))
    kb.button(text=texts.ADMIN_MEMBER_BAN, callback_data=AdminCb(section="member", action="ask", arg=f"ban:{user_id}"))
    kb.button(text=texts.ADMIN_MEMBER_KICK, callback_data=AdminCb(section="member", action="ask", arg=f"kick:{user_id}"))
    kb.button(text=texts.ADMIN_MEMBER_UNBAN, callback_data=AdminCb(section="member", action="do", arg=f"unban:{user_id}"))
    kb.adjust(2)
    _back_button(kb, "people", "list", "0")
    kb.adjust(1)
    return "\n".join(lines), kb.as_markup()


async def _drafts_list(session: AsyncSession, page: int) -> tuple[str, InlineKeyboardMarkup]:
    rows = await drafts.list_active(session)
    total = len(rows)
    page_rows = rows[page * PAGE : page * PAGE + PAGE]
    kb = InlineKeyboardBuilder()
    for d in page_rows:
        name = d.full_name or str(d.user_id)
        kind = texts.ADMIN_DRAFTS_ROW_BUG if d.kind == "bug" else texts.ADMIN_DRAFTS_ROW_FEATURE
        kb.button(text=f"{kind} {name}"[:40], callback_data=AdminCb(section="drafts", action="open", arg=str(d.user_id)))
    kb.adjust(1)
    _pager(kb, "drafts", page, total)
    _back_button(kb, "home")
    kb.adjust(1)
    return texts.ADMIN_DRAFTS_LIST.format(total=total), kb.as_markup()


async def _draft_card(session: AsyncSession, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    d = await drafts.get(session, user_id)
    kb = InlineKeyboardBuilder()
    _back_button(kb, "drafts", "list", "0")
    if d is None or d.status != "active":
        return texts.ADMIN_DRAFT_NOT_FOUND, kb.as_markup()
    kind = texts.ADMIN_DRAFT_KIND_BUG if d.kind == "bug" else texts.ADMIN_DRAFT_KIND_FEATURE
    lines = [
        texts.ADMIN_DRAFT_HEADER.format(kind=kind, user=user_html(d.user_id, d.full_name, d.username)),
        texts.ADMIN_DRAFT_TIMING.format(started=_since(d.started_at), updated=_since(d.updated_at)),
        texts.ADMIN_DRAFT_ATTACHMENTS.format(n=d.files_count),
        "",
    ]
    fields = d.fields or {}
    if not fields:
        lines.append(texts.ADMIN_DRAFT_EMPTY)
    for key, value in fields.items():
        if key == "logs":
            lines.append(texts.ADMIN_DRAFT_LOGS.format(n=len(value)))
        elif value:
            lines.append(texts.ADMIN_DRAFT_FIELD.format(title=FIELD_LABELS.get(key, key), value=escape(str(value))[:400]))
    return "\n".join(lines), kb.as_markup()


# ---------------------------------------------------------------- router


def build_router(settings: Settings) -> Router:
    router = Router(name="admin_menu")
    router.message.filter(F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(settings.admin_ids))
    router.callback_query.filter(F.from_user.id.in_(settings.admin_ids))

    async def _show(callback: CallbackQuery, rendered: tuple[str, InlineKeyboardMarkup]) -> None:
        text, markup = rendered
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(text, reply_markup=markup)
            except TelegramAPIError:
                await callback.message.answer(text, reply_markup=markup)

    @router.message(Command("admin"))
    async def admin_cmd(message: Message) -> None:
        text, markup = _home()
        await message.answer(text, reply_markup=markup)

    @router.callback_query(AdminCb.filter(F.section == "home"))
    async def home(callback: CallbackQuery) -> None:
        await callback.answer()
        await _show(callback, _home())

    @router.callback_query(AdminCb.filter(F.section == "buttons"))
    async def buttons(callback: CallbackQuery, callback_data: AdminCb, state: FSMContext, prefs: Prefs) -> None:
        a = callback_data.action
        if a == "list":
            await callback.answer()
            await _show(callback, await _buttons_list())
        elif a == "step":
            await callback.answer()
            await _show(callback, await _buttons_step(prefs, callback_data.arg))
        elif a == "del":
            step, _, idx = callback_data.arg.partition(":")
            await prefs.remove_button(step, int(idx))
            await callback.answer(texts.ADMIN_DELETED)
            await _show(callback, await _buttons_step(prefs, step))
        elif a == "add":
            step = callback_data.arg
            await state.set_state(AdminButtonForm.label)
            await state.set_data({"step": step})
            await callback.answer()
            if isinstance(callback.message, Message):
                await callback.message.answer(
                    texts.ADMIN_BUTTONS_ADD_PROMPT.format(step=escape(BUTTON_STEPS.get(step, step)))
                )

    @router.callback_query(AdminCb.filter(F.section == "notify"))
    async def notify(callback: CallbackQuery, callback_data: AdminCb, prefs: Prefs) -> None:
        if callback_data.action == "toggle":
            on = await prefs.toggle_notify(callback_data.arg)
            await callback.answer(texts.ADMIN_TOGGLE_ON if on else texts.ADMIN_TOGGLE_OFF)
        else:
            await callback.answer()
        await _show(callback, await _notify_list(prefs))

    @router.callback_query(AdminCb.filter(F.section == "logs"))
    async def logs_view(callback: CallbackQuery, callback_data: AdminCb, prefs: Prefs) -> None:
        if callback_data.action == "toggle":
            on = await prefs.toggle_log(callback_data.arg)
            await callback.answer(texts.ADMIN_TOGGLE_ON if on else texts.ADMIN_TOGGLE_OFF)
        else:
            await callback.answer()
        await _show(callback, await _logs_list(prefs))

    @router.callback_query(AdminCb.filter(F.section == "people"))
    async def people(callback: CallbackQuery, callback_data: AdminCb, session: AsyncSession) -> None:
        await callback.answer()
        await _show(callback, await _people_list(session, int(callback_data.arg or 0)))

    @router.callback_query(AdminCb.filter(F.section == "drafts"))
    async def drafts_view(callback: CallbackQuery, callback_data: AdminCb, session: AsyncSession) -> None:
        await callback.answer()
        if callback_data.action == "open":
            await _show(callback, await _draft_card(session, int(callback_data.arg)))
        else:
            await _show(callback, await _drafts_list(session, int(callback_data.arg or 0)))

    @router.callback_query(AdminCb.filter(F.section == "member"))
    async def member(
        callback: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot, tags: MemberTagResolver
    ) -> None:
        a = callback_data.action
        if a == "open":
            await callback.answer()
            await _show(callback, await _member_card(session, bot, settings, tags, int(callback_data.arg)))
        elif a == "ask":
            action, _, uid = callback_data.arg.partition(":")
            await callback.answer()
            await _show(callback, await _member_card(session, bot, settings, tags, int(uid), confirm=action))
        elif a == "do":
            parts = callback_data.arg.split(":")
            action, uid = parts[0], int(parts[1])
            minutes = int(parts[2]) if len(parts) > 2 else None
            if action not in members.ACTIONS:
                await callback.answer()
                return
            try:
                label = await members.apply_action(bot, settings.chat_id, uid, action, minutes)
                tags.invalidate(settings.chat_id, uid)
                await callback.answer(texts.ADMIN_MEMBER_DONE.format(label=label), show_alert=True)
            except TelegramAPIError as exc:
                await callback.answer(texts.ADMIN_MEMBER_FAILED.format(error=exc.message), show_alert=True)
            await _show(callback, await _member_card(session, bot, settings, tags, uid))

    # ------------------------------------------------------------ adding a button (FSM)

    @router.message(AdminButtonForm.label, F.text, ~F.text.startswith("/"))
    async def button_label(message: Message, state: FSMContext) -> None:
        await state.update_data(label=message.text.strip()[:64])
        await state.set_state(AdminButtonForm.value)
        await message.answer(texts.ADMIN_BUTTONS_ASK_VALUE)

    @router.message(AdminButtonForm.value, F.text, ~F.text.startswith("/"))
    async def button_value(message: Message, state: FSMContext, prefs: Prefs) -> None:
        data = await state.get_data()
        step = data.get("step", "")
        await prefs.add_button(step, data.get("label", message.text.strip()[:64]), message.text.strip()[:200])
        await state.clear()
        text, markup = await _buttons_step(prefs, step)
        await message.answer(texts.ADMIN_BUTTONS_ADDED)
        await message.answer(text, reply_markup=markup)

    return router
