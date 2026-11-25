from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage

from . import texts
from .commands import PrivateMenu, setup_commands
from .config import Settings, get_settings
from .db import init_db, make_engine, make_sessionmaker
from .handlers import build_root_router
from .middlewares import BackupMiddleware, DbSessionMiddleware, PrivateAccessMiddleware
from .services.llm import build_llm_client, build_summarizer
from .services.media import MediaDownloader
from .services.member_tags import MemberTagResolver
from .services.moderation import ModerationRules, RuleAnalyzer
from .services.prefs import Prefs
from .web.server import start_web

log = logging.getLogger("tradecloud_curator")

# Must be explicit: the backup needs edits, reactions and member join/leave events too,
# while resolve_used_update_types() only sees the types that have handlers.
ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "callback_query",
    "message_reaction",
    "message_reaction_count",
    "chat_member",
    "my_chat_member",
]


def make_storage(settings: Settings) -> BaseStorage:
    if settings.redis_url:
        from aiogram.fsm.storage.redis import RedisStorage

        return RedisStorage.from_url(settings.redis_url)
    return MemoryStorage()


async def check_bot_rights(bot: Bot, settings: Settings, bot_id: int) -> None:
    """Warn in the logs if the bot cannot delete commands / moderate — the most common cause of
    "group commands are not deleted"."""
    try:
        member = await bot.get_chat_member(settings.chat_id, bot_id)
    except TelegramAPIError as exc:
        log.warning(texts.LOG["main"]["rights_check_failed"], settings.chat_id, exc)
        return
    if member.status != ChatMemberStatus.ADMINISTRATOR:
        log.warning(texts.LOG["main"]["not_admin"], settings.chat_id, member.status)
        return
    if getattr(member, "can_delete_messages", None) is False and settings.delete_group_commands:
        log.warning(texts.LOG["main"]["no_delete_right"], settings.chat_id)


def make_bot(settings: Settings) -> Bot:
    session = None
    if settings.telegram_api_url:
        session = AiohttpSession(api=TelegramAPIServer.from_base(settings.telegram_api_url))
    return Bot(
        token=settings.bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    engine = make_engine(settings.database_url)
    await init_db(engine)
    sessionmaker = make_sessionmaker(engine)

    bot = make_bot(settings)
    me = await bot.me()
    await check_bot_rights(bot, settings, me.id)
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    media = MediaDownloader(
        bot, sessionmaker, settings.media_dir, settings.media_download_concurrency, settings.media_max_bytes
    )
    llm = build_llm_client(settings) if settings.llm_enabled else None
    if llm:
        await llm.resolve()  # resolve now so the target address shows up in the logs immediately
    rules = ModerationRules(
        allowed_domains=settings.allowed_domains,
        allowed_mentions=settings.allowed_mentions | {(me.username or "").lower()},
        block_mentions=settings.block_mentions,
    )

    tags = MemberTagResolver()
    summarizer = build_summarizer(settings, llm)
    prefs = Prefs(sessionmaker)
    dp = Dispatcher(
        storage=make_storage(settings),
        # Everything below is injected into handlers as keyword arguments.
        settings=settings,
        media=media,
        tags=tags,
        summarizer=summarizer,
        prefs=prefs,
        analyzers=[RuleAnalyzer()],  # TODO(llm): an LLM analyzer goes here
        bot_username=me.username,
    )
    dp.update.outer_middleware(BackupMiddleware(settings.chat_id, sessionmaker, media))
    dp.update.middleware(DbSessionMiddleware(sessionmaker))
    access = PrivateAccessMiddleware(settings, tags, PrivateMenu(settings))
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.include_router(build_root_router(settings, rules))

    web_runner = (
        await start_web(
            settings, sessionmaker, bot=bot, summarizer=summarizer, prefs=prefs, bot_username=me.username
        )
        if settings.web_enabled
        else None
    )
    await setup_commands(bot, settings)
    log.info(texts.LOG["main"]["started"], me.username, settings.chat_id)
    try:
        await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
    finally:
        if web_runner:
            await web_runner.cleanup()
        await bot.session.close()
        if llm:
            await llm.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
