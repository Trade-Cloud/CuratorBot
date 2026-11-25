"""Form drafts: one row per user, updated step by step.
Lets an admin see unfinished forms (who started /bug_report but never finished)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from aiogram.types import User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Draft
from ..utils.telegram import full_name
from ..web.events import bus


async def get(session: AsyncSession, user_id: int) -> Draft | None:
    return (await session.execute(select(Draft).where(Draft.user_id == user_id))).scalar_one_or_none()


async def start(session: AsyncSession, user: User, kind: str, started_at: datetime | None) -> bool:
    """Create/reset the user's draft to the active state. Returns True if this is really a new draft
    (no active one before) so admins are notified only once."""
    row = await get(session, user.id)
    is_new = row is None or row.status != "active"
    if row is None:
        row = Draft(user_id=user.id)
        session.add(row)
    row.username = user.username
    row.full_name = full_name(user)
    row.kind = kind
    row.fields = {}
    row.files_count = 0
    row.step = None
    row.status = "active"
    row.started_at = started_at
    await session.flush()
    bus.publish({"type": "draft", "user_id": user.id, "status": "active"})
    return is_new


async def update(
    session: AsyncSession,
    user_id: int,
    *,
    fields: dict[str, Any] | None = None,
    files_count: int | None = None,
    step: str | None = None,
) -> None:
    row = await get(session, user_id)
    if row is None or row.status != "active":
        return
    if fields is not None:
        row.fields = dict(fields)
    if files_count is not None:
        row.files_count = files_count
    if step is not None:
        row.step = step
    await session.flush()
    bus.publish({"type": "draft", "user_id": user_id, "status": "active"})


async def _finish(session: AsyncSession, user_id: int, status: str) -> None:
    row = await get(session, user_id)
    if row is None:
        return
    row.status = status
    await session.flush()
    bus.publish({"type": "draft", "user_id": user_id, "status": status})


async def mark_submitted(session: AsyncSession, user_id: int) -> None:
    await _finish(session, user_id, "submitted")


async def mark_cancelled(session: AsyncSession, user_id: int) -> None:
    await _finish(session, user_id, "cancelled")


async def list_active(session: AsyncSession) -> list[Draft]:
    return list(
        (await session.execute(select(Draft).where(Draft.status == "active").order_by(Draft.updated_at.desc()))).scalars()
    )
