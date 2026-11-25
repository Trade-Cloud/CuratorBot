"""Main moderation: delete contact sharing and solicitation, log to admins, mute repeat offenders."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatPermissions, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import texts
from ..config import Settings
from ..db.models import Violation
from ..services import backup
from ..services.member_tags import MemberTagResolver
from ..services.moderation import (
    Analyzer,
    ModerationRules,
    Severity,
    build_input,
    max_severity,
    run_analyzers,
)
from ..services.notify import notify_admins
from ..web.events import bus
from ..utils.telegram import delete_later, thread_param, topic_of, truncate, user_html_from

log = logging.getLogger(__name__)


def _has_content(message: Message) -> bool:
    return bool(
        message.text or message.caption or message.contact or message.forward_origin or message.via_bot
    )


def build_router(settings: Settings, rules: ModerationRules) -> Router:
    router = Router(name="moderation")
    router.message.filter(F.chat.id == settings.chat_id)
    router.edited_message.filter(F.chat.id == settings.chat_id)

    async def moderate(
        message: Message,
        bot: Bot,
        session: AsyncSession,
        tags: MemberTagResolver,
        analyzers: list[Analyzer],
    ) -> None:
        if not settings.moderation_enabled or topic_of(message) not in settings.moderation_topics:
            return
        if not _has_content(message) or await tags.is_exempt(message, settings):
            return

        findings = await run_analyzers(analyzers, build_input(message), rules)
        severity = max_severity(findings)
        if severity is None:
            return
        action = settings.moderation_hard_action if severity == Severity.HARD else settings.moderation_soft_action
        if action == "ignore":
            return

        user = message.from_user
        if user is None:
            return
        result = "logged"
        if action == "delete":
            try:
                await message.delete()
                result = "deleted"
                await backup.mark_deleted(session, message.chat.id, message.message_id)
                bus.publish({"type": "deleted", "chat_id": message.chat.id, "message_id": message.message_id})
            except TelegramAPIError as exc:
                log.warning(texts.LOG["moderation"]["delete_failed"], message.message_id, exc)

        session.add(
            Violation(
                chat_id=message.chat.id,
                thread_id=topic_of(message),
                user_id=user.id,
                message_id=message.message_id,
                severity="hard" if severity == Severity.HARD else "soft",
                findings=[f.as_dict() for f in findings],
                text=message.text or message.caption,
                action=result,
            )
        )
        await session.flush()
        bus.publish({"type": "violation", "user_id": user.id, "severity": "hard" if severity == Severity.HARD else "soft"})

        mention = user_html_from(user)
        if result == "deleted":
            warn = await bot.send_message(
                message.chat.id,
                texts.MOD_WARNING.format(user=mention),
                message_thread_id=thread_param(topic_of(message)),
            )
            delete_later(warn, settings.moderation_warning_ttl)
            if await _maybe_mute(bot, session, message, mention):
                result = "muted"

        reasons = ", ".join(sorted({f"{f.kind}: {f.match}" for f in findings}))
        content = escape(truncate(message.text or message.caption or "", 1500))
        await notify_admins(
            bot,
            settings,
            texts.MOD_ADMIN_REPORT.format(
                result=result,
                severity=severity.name.lower(),
                user=mention,
                user_id=user.id,
                reasons=escape(reasons),
                content=content,
            ),
        )

    async def _maybe_mute(bot: Bot, session: AsyncSession, message: Message, mention: str) -> bool:
        if settings.moderation_mute_after <= 0 or message.from_user is None:
            return False
        since = datetime.now(timezone.utc) - timedelta(hours=settings.moderation_mute_window_hours)
        count = await session.scalar(
            select(func.count(Violation.id)).where(
                Violation.chat_id == message.chat.id,
                Violation.user_id == message.from_user.id,
                Violation.severity == "hard",
                Violation.created_at >= since,
            )
        )
        if (count or 0) < settings.moderation_mute_after:
            return False
        try:
            await bot.restrict_chat_member(
                message.chat.id,
                message.from_user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=datetime.now(timezone.utc) + timedelta(minutes=settings.moderation_mute_minutes),
            )
        except TelegramAPIError as exc:
            log.warning(texts.LOG["moderation"]["mute_failed"], message.from_user.id, exc)
            return False
        sent = await bot.send_message(
            message.chat.id,
            texts.MOD_MUTED.format(user=mention, minutes=settings.moderation_mute_minutes),
            message_thread_id=thread_param(topic_of(message)),
        )
        delete_later(sent, settings.moderation_warning_ttl)
        return True

    router.message.register(moderate)
    router.edited_message.register(moderate)
    return router
