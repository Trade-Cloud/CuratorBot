"""/start (в том числе deep-link'и из чата) и просмотр репорта в ЛС."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from .. import texts
from ..config import Settings
from ..services import reports
from ..services.member_tags import MemberTagResolver
from ..utils.ids import normalize_public_id
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


@router.message(CommandStart(deep_link=True))
async def start_deep_link(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    tags: MemberTagResolver,
) -> None:
    payload = command.args or ""
    if payload == "bug":
        await begin_from_private(message, state, bot, settings, tags, reports.KIND_BUG)
    elif payload == "feature":
        await begin_from_private(message, state, bot, settings, tags, reports.KIND_FEATURE)
    elif payload.startswith("r_"):
        await show_report(message, payload[2:], bot, session, settings)
    else:
        await start(message, settings)


@router.message(CommandStart())
async def start(message: Message, settings: Settings) -> None:
    text = texts.START_TEXT
    if message.from_user and settings.is_admin(message.from_user.id):
        text += texts.ADMIN_HELP
    await message.answer(text)


@router.message(Command("help"))
async def help_(message: Message, settings: Settings) -> None:
    await start(message, settings)


@router.message(Command("report"))
async def report_cmd(
    message: Message, command: CommandObject, bot: Bot, session: AsyncSession, settings: Settings
) -> None:
    await show_report(message, command.args or "", bot, session, settings)
