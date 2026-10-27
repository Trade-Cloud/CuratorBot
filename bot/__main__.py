from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage

from .commands import PrivateMenu, setup_commands
from .config import Settings, get_settings
from .db import init_db, make_engine, make_sessionmaker
from .handlers import build_root_router
from .middlewares import BackupMiddleware, DbSessionMiddleware, PrivateAccessMiddleware
from .services.llm import build_llm_client, build_summarizer
from .services.media import MediaDownloader
from .services.member_tags import MemberTagResolver
from .services.moderation import ModerationRules, RuleAnalyzer
from .web.server import start_web

log = logging.getLogger("beta_bot")

# Нужно явно: бекапу нужны и правки, и реакции, и вход/выход участников,
# а resolve_used_update_types() видит только типы, на которые есть хендлеры.
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
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    media = MediaDownloader(
        bot, sessionmaker, settings.media_dir, settings.media_download_concurrency, settings.media_max_bytes
    )
    llm = build_llm_client(settings) if settings.llm_enabled else None
    if llm:
        await llm.resolve()  # просто чтобы в логах сразу было видно, куда пойдём
    rules = ModerationRules(
        allowed_domains=settings.allowed_domains,
        allowed_mentions=settings.allowed_mentions | {(me.username or "").lower()},
        block_mentions=settings.block_mentions,
    )

    tags = MemberTagResolver()
    dp = Dispatcher(
        storage=make_storage(settings),
        # Всё ниже приходит в хендлеры как именованные аргументы.
        settings=settings,
        media=media,
        tags=tags,
        summarizer=build_summarizer(settings, llm),
        analyzers=[RuleAnalyzer()],  # TODO(llm): сюда добавится LLM-анализатор
        bot_username=me.username,
    )
    dp.update.outer_middleware(BackupMiddleware(settings.chat_id, sessionmaker, media))
    dp.update.middleware(DbSessionMiddleware(sessionmaker))
    access = PrivateAccessMiddleware(settings, tags, PrivateMenu(settings))
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.include_router(build_root_router(settings, rules))

    web_runner = await start_web(settings, sessionmaker) if settings.web_enabled else None
    await setup_commands(bot, settings)
    log.info("started as @%s, chat %s", me.username, settings.chat_id)
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
