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
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat

from .config import Settings, get_settings
from .db import init_db, make_engine, make_sessionmaker
from .handlers import build_root_router
from .middlewares import BackupMiddleware, DbSessionMiddleware
from .services.llm import build_summarizer
from .services.media import MediaDownloader
from .services.member_tags import MemberTagResolver
from .services.moderation import ModerationRules, RuleAnalyzer

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


async def set_commands(bot: Bot, settings: Settings) -> None:
    public = [
        BotCommand(command="bug_report", description="Сообщить о баге"),
        BotCommand(command="feature", description="Предложить идею"),
    ]
    await bot.set_my_commands(public, scope=BotCommandScopeChat(chat_id=settings.chat_id))
    await bot.set_my_commands(
        [*public, BotCommand(command="cancel", description="Отменить анкету")],
        scope=BotCommandScopeAllPrivateChats(),
    )
    admin_cmds = [
        *public,
        BotCommand(command="cancel", description="Отменить анкету"),
        BotCommand(command="changelog", description="Опубликовать changelog"),
        BotCommand(command="status", description="Статус репорта: ID статус [комментарий]"),
        BotCommand(command="report", description="Открыть репорт по ID"),
    ]
    for admin_id in settings.admin_ids:
        try:
            await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:  # админ мог ещё не запускать бота
            log.info("cannot set commands for admin %s", admin_id)


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
    rules = ModerationRules(
        allowed_domains=settings.allowed_domains,
        allowed_mentions=settings.allowed_mentions | {(me.username or "").lower()},
        block_mentions=settings.block_mentions,
    )

    dp = Dispatcher(
        storage=make_storage(settings),
        # Всё ниже приходит в хендлеры как именованные аргументы.
        settings=settings,
        media=media,
        tags=MemberTagResolver(),
        summarizer=build_summarizer(settings.llm_enabled),
        analyzers=[RuleAnalyzer()],  # TODO(llm): сюда добавится LLM-анализатор
        bot_username=me.username,
    )
    dp.update.outer_middleware(BackupMiddleware(settings.chat_id, sessionmaker, media))
    dp.update.middleware(DbSessionMiddleware(sessionmaker))
    dp.include_router(build_root_router(settings, rules))

    await set_commands(bot, settings)
    log.info("started as @%s, chat %s", me.username, settings.chat_id)
    try:
        await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
