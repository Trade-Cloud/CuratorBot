"""Queries for the web viewer: everything is returned as JSON dicts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import Attachment, ChatMessage, ChatUser, Draft, MessageVersion, Report, Topic, Violation
from ..utils.telegram import post_link

CONFIGURED_NAMES = (
    ("topic_info_id", "INFO"),
    ("topic_changelog_id", "Changelog"),
    ("topic_bugs_id", "Bug Reports"),
    ("topic_features_id", "Feature Requests"),
    ("topic_main_id", "Main"),
    ("topic_logs_id", "Logs"),
)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _user_dict(user: ChatUser | None, user_id: int | None) -> dict[str, Any] | None:
    if user_id is None:
        return None
    if user is None:
        return {"id": user_id, "name": str(user_id), "username": None, "tag": None, "is_bot": False}
    name = " ".join(p for p in (user.first_name, user.last_name) if p) or str(user.id)
    return {"id": user.id, "name": name, "username": user.username, "tag": user.last_tag, "is_bot": user.is_bot}


def _att_dict(att: Attachment) -> dict[str, Any]:
    return {
        "id": att.id,
        "kind": att.kind,
        "file_name": att.file_name,
        "mime_type": att.mime_type,
        "size": att.file_size,
        "status": att.status,
        "url": f"/api/media/{att.id}" if att.status == "done" else None,
    }


async def topic_names(session: AsyncSession, settings: Settings) -> dict[int, str]:
    names: dict[int, str] = {0: "General"}
    for attr, label in CONFIGURED_NAMES:
        value = getattr(settings, attr)
        if value is not None:
            names[value] = label
    rows = await session.execute(select(Topic.thread_id, Topic.name).where(Topic.chat_id == settings.chat_id))
    for thread_id, name in rows:
        if name:
            names[thread_id] = name
    return names


async def list_topics(session: AsyncSession, settings: Settings) -> list[dict[str, Any]]:
    names = await topic_names(session, settings)
    rows = await session.execute(
        select(ChatMessage.thread_id, func.count(ChatMessage.id), func.max(ChatMessage.date))
        .where(ChatMessage.chat_id == settings.chat_id)
        .group_by(ChatMessage.thread_id)
    )
    stats = {thread_id: (count, last) for thread_id, count, last in rows}
    ids = set(names) | set(stats)
    result = [
        {
            "id": tid,
            "name": names.get(tid, f"Topic {tid}"),
            "count": stats.get(tid, (0, None))[0],
            "last": _iso(stats.get(tid, (0, None))[1]),
        }
        for tid in ids
    ]
    return sorted(result, key=lambda t: (t["id"] != 0, t["name"].lower()))


async def serialize_messages(
    session: AsyncSession, rows: list[ChatMessage], names: dict[int, str]
) -> list[dict[str, Any]]:
    if not rows:
        return []
    pks = [r.id for r in rows]
    user_ids = {r.user_id for r in rows if r.user_id}

    attachments: dict[int, list[dict[str, Any]]] = {}
    for att in (
        await session.execute(
            select(Attachment).where(Attachment.message_pk.in_(pks), Attachment.replaced_by_edit.is_not(True))
        )
    ).scalars():
        attachments.setdefault(att.message_pk, []).append(_att_dict(att))

    # The latest edit — to show "was: …" right in the card.
    last_versions: dict[int, MessageVersion] = {}
    edited_pks = [r.id for r in rows if r.is_edited]
    if edited_pks:
        latest = (
            select(MessageVersion.message_pk, func.max(MessageVersion.version).label("v"))
            .where(MessageVersion.message_pk.in_(edited_pks))
            .group_by(MessageVersion.message_pk)
            .subquery()
        )
        for v in (
            await session.execute(
                select(MessageVersion).join(
                    latest, and_(MessageVersion.message_pk == latest.c.message_pk, MessageVersion.version == latest.c.v)
                )
            )
        ).scalars():
            last_versions[v.message_pk] = v

    reply_keys = {(r.reply_to_chat_id or r.chat_id, r.reply_to_message_id) for r in rows if r.reply_to_message_id}
    replies: dict[tuple[int, int], ChatMessage] = {}
    if reply_keys:
        for rep in (
            await session.execute(
                select(ChatMessage).where(tuple_(ChatMessage.chat_id, ChatMessage.message_id).in_(list(reply_keys)))
            )
        ).scalars():
            replies[(rep.chat_id, rep.message_id)] = rep
            if rep.user_id:
                user_ids.add(rep.user_id)

    users = {
        u.id: u for u in (await session.execute(select(ChatUser).where(ChatUser.id.in_(user_ids)))).scalars()
    } if user_ids else {}

    result = []
    for r in rows:
        reply = None
        # A reply to the topic's root message is just "a message in the topic" — not shown as a reply.
        if r.reply_to_message_id and r.reply_to_message_id != r.thread_id:
            rep = replies.get((r.reply_to_chat_id or r.chat_id, r.reply_to_message_id))
            reply = {
                "message_id": r.reply_to_message_id,
                "text": (rep.text or f"[{rep.content_type}]")[:200] if rep else None,
                "user": _user_dict(users.get(rep.user_id), rep.user_id) if rep else None,
            }
        result.append(
            {
                "pk": r.id,
                "message_id": r.message_id,
                "thread_id": r.thread_id,
                "topic": names.get(r.thread_id, f"Topic {r.thread_id}"),
                "link": post_link(r.chat_id, r.message_id, r.thread_id),
                "date": _iso(r.date),
                "user": _user_dict(users.get(r.user_id), r.user_id),
                "sender_chat_id": r.sender_chat_id,
                "sender_tag": r.sender_tag,
                "content_type": r.content_type,
                "text": r.text,
                "entities": r.entities or [],
                "quote": r.quote_text,
                "reply": reply,
                "forward": r.forward_origin,
                "media_group_id": r.media_group_id,
                "edited": r.is_edited,
                "edit_count": r.edit_count,
                "last_edit": _iso(r.last_edit_date),
                "deleted_by_bot": r.deleted_by_bot,
                "last_change": _version_brief(last_versions.get(r.id)),
                "attachments": attachments.get(r.id, []),
            }
        )
    return result


def _version_brief(v: MessageVersion | None) -> dict[str, Any] | None:
    if v is None:
        return None
    media_changed = (v.old_attachments or []) != (v.new_attachments or []) and v.old_attachments is not None
    return {
        "version": v.version,
        "old_text": v.old_text,
        "text_changed": v.old_text != v.new_text,
        "media_changed": media_changed,
        "date": _iso(v.edit_date),
    }


async def list_messages(
    session: AsyncSession,
    settings: Settings,
    *,
    thread: int | None = None,
    before: int | None = None,
    after: int | None = None,
    user_id: int | None = None,
    q: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    stmt = select(ChatMessage).where(ChatMessage.chat_id == settings.chat_id)
    if thread is not None:
        stmt = stmt.where(ChatMessage.thread_id == thread)
    if user_id is not None:
        stmt = stmt.where(ChatMessage.user_id == user_id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(ChatMessage.text.ilike(like))
    if before is not None:
        stmt = stmt.where(ChatMessage.id < before)
    if after is not None:
        stmt = stmt.where(ChatMessage.id > after)
        stmt = stmt.order_by(ChatMessage.id.asc()).limit(limit)
        rows = list((await session.execute(stmt)).scalars())
    else:
        stmt = stmt.order_by(ChatMessage.id.desc()).limit(limit)
        rows = list(reversed((await session.execute(stmt)).scalars().all()))
    return await serialize_messages(session, rows, await topic_names(session, settings))


async def get_message(session: AsyncSession, settings: Settings, *, pk: int | None = None,
                      chat_id: int | None = None, message_id: int | None = None) -> dict[str, Any] | None:
    stmt = select(ChatMessage)
    if pk is not None:
        stmt = stmt.where(ChatMessage.id == pk)
    else:
        stmt = stmt.where(and_(ChatMessage.chat_id == chat_id, ChatMessage.message_id == message_id))
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    return (await serialize_messages(session, [row], await topic_names(session, settings)))[0]


async def _attachments_by_id(session: AsyncSession, ids: set[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    return {a.id: _att_dict(a) for a in (await session.execute(select(Attachment).where(Attachment.id.in_(ids)))).scalars()}


def _version_dict(v: MessageVersion, atts: dict[int, dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": v.id,
        "message_pk": v.message_pk,
        "version": v.version,
        "old": v.old_text,
        "new": v.new_text,
        "old_type": v.old_content_type,
        "new_type": v.new_content_type,
        "old_attachments": [atts[i] for i in (v.old_attachments or []) if i in atts],
        "new_attachments": [atts[i] for i in (v.new_attachments or []) if i in atts],
        "media_changed": v.old_attachments is not None and (v.old_attachments or []) != (v.new_attachments or []),
        "date": _iso(v.edit_date),
    }


async def message_versions(session: AsyncSession, pk: int) -> list[dict[str, Any]]:
    rows = list(
        (
            await session.execute(
                select(MessageVersion).where(MessageVersion.message_pk == pk).order_by(MessageVersion.version)
            )
        ).scalars()
    )
    ids = {i for v in rows for i in (v.old_attachments or []) + (v.new_attachments or [])}
    atts = await _attachments_by_id(session, ids)
    return [_version_dict(v, atts) for v in rows]


async def list_edits(
    session: AsyncSession, settings: Settings, *, version_id: int | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    """Edit feed: each edit plus the message it belongs to."""
    stmt = (
        select(MessageVersion, ChatMessage)
        .join(ChatMessage, ChatMessage.id == MessageVersion.message_pk)
        .where(ChatMessage.chat_id == settings.chat_id)
    )
    if version_id is not None:
        stmt = stmt.where(MessageVersion.id == version_id)
    rows = (await session.execute(stmt.order_by(MessageVersion.id.desc()).limit(limit))).all()
    if not rows:
        return []
    ids = {i for v, _ in rows for i in (v.old_attachments or []) + (v.new_attachments or [])}
    atts = await _attachments_by_id(session, ids)
    names = await topic_names(session, settings)
    user_ids = {m.user_id for _, m in rows if m.user_id}
    users = {
        u.id: u for u in (await session.execute(select(ChatUser).where(ChatUser.id.in_(user_ids)))).scalars()
    } if user_ids else {}
    return [
        {
            **_version_dict(v, atts),
            "message_id": m.message_id,
            "thread_id": m.thread_id,
            "topic": names.get(m.thread_id, f"Topic {m.thread_id}"),
            "link": post_link(m.chat_id, m.message_id, m.thread_id),
            "user": _user_dict(users.get(m.user_id), m.user_id),
        }
        for v, m in rows
    ]


async def list_users(session: AsyncSession, settings: Settings) -> list[dict[str, Any]]:
    counts = dict(
        (
            await session.execute(
                select(ChatMessage.user_id, func.count(ChatMessage.id))
                .where(ChatMessage.chat_id == settings.chat_id, ChatMessage.user_id.is_not(None))
                .group_by(ChatMessage.user_id)
            )
        ).all()
    )
    users = (await session.execute(select(ChatUser).order_by(ChatUser.last_seen.desc()))).scalars()
    return [
        {**_user_dict(u, u.id), "messages": counts.get(u.id, 0), "first_seen": _iso(u.first_seen),
         "last_seen": _iso(u.last_seen)}  # type: ignore[dict-item]
        for u in users
    ]


async def list_reports(
    session: AsyncSession, settings: Settings | None = None, bot_username: str | None = None
) -> list[dict[str, Any]]:
    rows = list((await session.execute(select(Report).order_by(Report.id.desc()).limit(500))).scalars())
    counts = dict(
        (
            await session.execute(
                select(Attachment.report_id, func.count(Attachment.id))
                .where(Attachment.report_id.is_not(None))
                .group_by(Attachment.report_id)
            )
        ).all()
    )
    return [
        {
            "id": r.public_id, "kind": r.kind, "status": r.status, "status_comment": r.status_comment,
            "user": {"id": r.user_id, "name": r.full_name, "username": r.username},
            "fields": r.fields, "created": _iso(r.created_at), "updated": _iso(r.updated_at),
            "attachments": counts.get(r.id, 0),
            "post_link": post_link(r.post_chat_id, r.post_message_id, r.post_thread_id) if r.post_chat_id and r.post_message_id else None,
            "bot_link": f"https://t.me/{bot_username}?start=r_{r.public_id}" if bot_username else None,
        }
        for r in rows
    ]


async def list_drafts(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await session.execute(select(Draft).where(Draft.status == "active").order_by(Draft.updated_at.desc()))
    ).scalars()
    return [
        {
            "user": {"id": d.user_id, "name": d.full_name, "username": d.username},
            "kind": d.kind, "fields": d.fields or {}, "files": d.files_count, "step": d.step,
            "started": _iso(d.started_at), "updated": _iso(d.updated_at),
        }
        for d in rows
    ]


async def list_violations(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(select(Violation).order_by(Violation.id.desc()).limit(500))).scalars().all()
    user_ids = {v.user_id for v in rows}
    users = {
        u.id: u for u in (await session.execute(select(ChatUser).where(ChatUser.id.in_(user_ids)))).scalars()
    } if user_ids else {}
    return [
        {
            "id": v.id, "user": _user_dict(users.get(v.user_id), v.user_id), "severity": v.severity,
            "action": v.action, "findings": v.findings, "text": v.text, "thread_id": v.thread_id,
            "message_id": v.message_id, "created": _iso(v.created_at),
        }
        for v in rows
    ]


async def attachment_path(session: AsyncSession, att_id: int) -> tuple[str, str | None, str | None] | None:
    att = await session.get(Attachment, att_id)
    if att is None or att.status != "done" or not att.local_path:
        return None
    return att.local_path, att.mime_type, att.file_name


__all__ = [
    "get_message", "list_messages", "list_reports", "list_drafts", "list_topics", "list_users",
    "list_violations", "message_versions", "attachment_path", "list_edits",
]
