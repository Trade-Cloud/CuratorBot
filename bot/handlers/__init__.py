from aiogram import Router

from ..config import Settings
from ..services.moderation import ModerationRules
from . import admin, admin_menu, edit, forms, group, moderation, private


def build_root_router(settings: Settings, rules: ModerationRules) -> Router:
    root = Router(name="root")
    # Order matters: moderation is last — it catches everything the commands did not take.
    root.include_routers(
        admin.build_router(settings),
        admin_menu.build_router(settings),
        group.build_router(settings),
        private.router,
        edit.router,
        forms.router,
        moderation.build_router(settings, rules),
    )
    return root
