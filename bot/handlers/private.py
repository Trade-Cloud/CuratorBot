"""/start (including deep links from the chat) and viewing a report in private chat."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import keyboards, texts
from ..config import Settings
from ..db.models import ChatUser
from ..services import reports
from ..services.member_tags import MemberTagResolver
from ..services.notify import announce
from ..services.prefs import Prefs
from ..utils.ids import normalize_public_id
from ..utils.telegram import user_html_from
from .forms import begin_from_private

router = Router(name="private")
router.message.filter(F.chat.type == ChatType.PRIVATE)


async def show_report(message: Message, raw_id: str, bot: Bot, session: AsyncSession, settings: Settings) -> None:
    assert message.from_user
    rid = normalize_public_id(raw_id)
    report = await reports.get_report(session, rid) if rid else None
    if report is None:
        await message.answer(texts.REPORT_NOT_FOUND)
        return
    if not reports.can_view(report, message.from_user.id, settings):
        await message.answer(texts.DETAILS_DENIED)
        return
    await reports.send_details(bot, message.chat.id, session, report, settings)
    # The author (and admins) can edit the report right here.
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.EDIT_BUTTON, callback_data=keyboards.EditCb(rid=report.public_id, action="menu"))
    await message.answer(texts.EDIT_PROMPT, reply_markup=kb.as_markup())


async def _mark_started(
    message: Message, bot: Bot, session: AsyncSession, settings: Settings, prefs: Prefs
) -> None:
    """Record that the person opened the bot's private chat and notify admins once."""
    user = message.from_user
    if user is None:
        return
    row = await session.get(ChatUser, user.id)
    if row is None:
        session.add(
            ChatUser(
                id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                is_bot=user.is_bot,
                started_bot=True,
            )
        )
        first_time = True
    else:
        first_time = not row.started_bot
        row.started_bot = True
    if first_time:
        await announce(bot, settings, prefs, "user_start", texts.USER_STARTED.format(user=user_html_from(user)))


@router.message(CommandStart(deep_link=True))
async def start_deep_link(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    tags: MemberTagResolver,
    prefs: Prefs,
) -> None:
    payload = command.args or ""
    if payload == "bug":
        await begin_from_private(message, state, bot, settings, tags, prefs, session, reports.KIND_BUG)
    elif payload == "feature":
        await begin_from_private(message, state, bot, settings, tags, prefs, session, reports.KIND_FEATURE)
    elif payload.startswith("r_"):
        await show_report(message, payload[2:], bot, session, settings)
    else:
        await start(message, settings, bot, session, prefs)


@router.message(CommandStart())
async def start(message: Message, settings: Settings, bot: Bot, session: AsyncSession, prefs: Prefs) -> None:
    await _mark_started(message, bot, session, settings, prefs)
    text = texts.START_TEXT
    if message.from_user and settings.is_admin(message.from_user.id):
        text += texts.ADMIN_HELP
    await message.answer(text)


@router.message(Command("help"))
async def help_(message: Message, settings: Settings, bot: Bot, session: AsyncSession, prefs: Prefs) -> None:
    await start(message, settings, bot, session, prefs)


@router.message(Command("report"))
async def report_cmd(
    message: Message, command: CommandObject, bot: Bot, session: AsyncSession, settings: Settings
) -> None:
    await show_report(message, command.args or "", bot, session, settings)
