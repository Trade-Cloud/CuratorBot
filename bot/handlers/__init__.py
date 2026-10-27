from aiogram import Router

from ..config import Settings
from ..services.moderation import ModerationRules
from . import admin, forms, group, moderation, private


def build_root_router(settings: Settings, rules: ModerationRules) -> Router:
    root = Router(name="root")
    # Порядок важен: модерация последней — она ловит всё, что не забрали команды.
    root.include_routers(
        admin.build_router(settings),
        group.build_router(settings),
        private.router,
        forms.router,
        moderation.build_router(settings, rules),
    )
    return root
