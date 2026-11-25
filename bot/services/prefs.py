"""Runtime settings changed from the admin menu: notification toggles and form quick buttons.

Stored in the app_settings table (key -> JSON). Prefs keeps an in-memory cache (single process),
so both the bot handlers and the web server share one object.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .. import texts
from ..config import CONFIG
from ..db.models import AppSetting

# Notification event -> admin-menu label. Order = order in the menu.
NOTIFY_EVENTS: dict[str, str] = texts.NOTIFY_EVENTS

# Form steps that can have quick-reply buttons -> admin-menu label.
BUTTON_STEPS: dict[str, str] = texts.BUTTON_STEPS

# Default quick-reply buttons per step (an admin can change them in the admin menu).
DEFAULT_BUTTONS: dict[str, list[dict[str, str]]] = CONFIG["reports"]["default_buttons"]

NOTIFY_KEY = "notify"
LOGS_KEY = "logs"
BUTTONS_KEY = "buttons"

# The logs topic receives the same set of events as notifications (but short, without details).
LOG_EVENTS = NOTIFY_EVENTS


class Prefs:
    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sm = sessionmaker
        self._cache: dict[str, Any] | None = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> dict[str, Any]:
        if self._cache is None:
            async with self._lock:
                if self._cache is None:
                    async with self._sm() as session:
                        rows = (await session.execute(select(AppSetting))).scalars()
                        self._cache = {r.key: r.value for r in rows}
        return self._cache

    async def _save(self, key: str, value: Any) -> None:
        cache = await self._ensure()
        async with self._sm() as session:
            row = await session.get(AppSetting, key)
            if row is None:
                session.add(AppSetting(key=key, value=value))
            else:
                row.value = value
            await session.commit()
        cache[key] = value

    # -------------------------------------------------------------- notifications

    async def all_notify(self) -> dict[str, bool]:
        stored = (await self._ensure()).get(NOTIFY_KEY) or {}
        return {event: bool(stored.get(event, True)) for event in NOTIFY_EVENTS}

    async def get_notify(self, event: str) -> bool:
        stored = (await self._ensure()).get(NOTIFY_KEY) or {}
        return bool(stored.get(event, True))

    async def set_notify(self, event: str, on: bool) -> None:
        stored = dict((await self._ensure()).get(NOTIFY_KEY) or {})
        stored[event] = bool(on)
        await self._save(NOTIFY_KEY, stored)

    async def toggle_notify(self, event: str) -> bool:
        new = not await self.get_notify(event)
        await self.set_notify(event, new)
        return new

    # -------------------------------------------------------------- logs topic

    async def all_logs(self) -> dict[str, bool]:
        stored = (await self._ensure()).get(LOGS_KEY) or {}
        return {event: bool(stored.get(event, True)) for event in LOG_EVENTS}

    async def get_log(self, event: str) -> bool:
        stored = (await self._ensure()).get(LOGS_KEY) or {}
        return bool(stored.get(event, True))

    async def set_log(self, event: str, on: bool) -> None:
        stored = dict((await self._ensure()).get(LOGS_KEY) or {})
        stored[event] = bool(on)
        await self._save(LOGS_KEY, stored)

    async def toggle_log(self, event: str) -> bool:
        new = not await self.get_log(event)
        await self.set_log(event, new)
        return new

    # -------------------------------------------------------------- form quick buttons

    async def all_buttons(self) -> dict[str, list[dict[str, str]]]:
        stored = (await self._ensure()).get(BUTTONS_KEY) or {}
        # stored overrides the defaults per step: if an admin removed "Browser", an empty list remains.
        return {**DEFAULT_BUTTONS, **stored}

    async def list_buttons(self, step: str) -> list[dict[str, str]]:
        return (await self.all_buttons()).get(step, [])

    async def _save_buttons(self, step: str, buttons: list[dict[str, str]]) -> None:
        stored = dict((await self._ensure()).get(BUTTONS_KEY) or {})
        stored[step] = buttons
        await self._save(BUTTONS_KEY, stored)

    async def add_button(self, step: str, label: str, value: str) -> None:
        buttons = list(await self.list_buttons(step))
        buttons.append({"label": label, "value": value})
        await self._save_buttons(step, buttons)

    async def remove_button(self, step: str, index: int) -> bool:
        buttons = list(await self.list_buttons(step))
        if 0 <= index < len(buttons):
            buttons.pop(index)
            await self._save_buttons(step, buttons)
            return True
        return False
