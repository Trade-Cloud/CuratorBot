"""Building the root router: catches handler registration errors (imports, filters)
across all routers — things a plain module import does not reveal."""

from bot.config import Settings
from bot.handlers import build_root_router
from bot.services.moderation import ModerationRules


def test_build_root_router():
    # conftest set BOT_TOKEN and CHAT_ID in the environment.
    settings = Settings()
    rules = ModerationRules(allowed_domains=set(), allowed_mentions=set(), block_mentions=True)
    root = build_root_router(settings, rules)
    names = [sub.name for sub in root.sub_routers]
    assert names == ["admin", "admin_menu", "group", "private", "edit", "forms", "moderation"]
