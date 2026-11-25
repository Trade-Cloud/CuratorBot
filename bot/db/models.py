from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JsonType = JSON().with_variant(JSONB(), "postgresql")


# In SQLite (tests) autoincrement only works on INTEGER PRIMARY KEY; in Postgres it stays BIGINT.
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JsonType, list[Any]: JsonType}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------- backup


class ChatUser(Base):
    """Last known member profile."""

    __tablename__ = "chat_users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(256))
    last_name: Mapped[str | None] = mapped_column(String(256))
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    language_code: Mapped[str | None] = mapped_column(String(16))
    last_tag: Mapped[str | None] = mapped_column(String(64))
    # Whether the person has ever opened the bot's private chat (for the one-time "started the bot" notice).
    started_bot: Mapped[bool | None] = mapped_column(Boolean)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())


class Topic(Base):
    __tablename__ = "topics"
    __table_args__ = (UniqueConstraint("chat_id", "thread_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    thread_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str | None] = mapped_column(String(256))
    icon_color: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), onupdate=utcnow
    )


class RawUpdate(TimestampMixin, Base):
    """The full raw update — a fallback in case something was not parsed into a structure."""

    __tablename__ = "raw_updates"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    update_id: Mapped[int] = mapped_column(BigInteger, index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    chat_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    payload: Mapped[dict[str, Any]]


class ChatMessage(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("chat_id", "message_id"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    thread_id: Mapped[int] = mapped_column(BigInteger, index=True)  # 0 = General
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    sender_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    sender_tag: Mapped[str | None] = mapped_column(String(64))
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    content_type: Mapped[str] = mapped_column(String(32))
    text: Mapped[str | None] = mapped_column(Text)
    html_text: Mapped[str | None] = mapped_column(Text)
    entities: Mapped[list[Any] | None]
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger)
    reply_to_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    quote_text: Mapped[str | None] = mapped_column(Text)
    forward_origin: Mapped[dict[str, Any] | None]
    media_group_id: Mapped[str | None] = mapped_column(String(64))
    via_bot_id: Mapped[int | None] = mapped_column(BigInteger)
    is_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    edit_count: Mapped[int] = mapped_column(Integer, default=0)
    last_edit_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Deleted by moderation (Telegram does not send deletions to bots).
    deleted_by_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    raw: Mapped[dict[str, Any]]
    saved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())

    versions: Mapped[list[MessageVersion]] = relationship(
        back_populates="message", order_by="MessageVersion.version"
    )


class MessageVersion(Base):
    """Edit history: old and new text, one row per edit."""

    __tablename__ = "message_versions"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    message_pk: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    old_text: Mapped[str | None] = mapped_column(Text)
    new_text: Mapped[str | None] = mapped_column(Text)
    old_entities: Mapped[list[Any] | None]
    new_entities: Mapped[list[Any] | None]
    # Media replaced by an edit: message type and attachment ids (attachments.id) before and after.
    old_content_type: Mapped[str | None] = mapped_column(String(32))
    new_content_type: Mapped[str | None] = mapped_column(String(32))
    old_attachments: Mapped[list[Any] | None]
    new_attachments: Mapped[list[Any] | None]
    edit_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict[str, Any]]
    saved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())

    message: Mapped[ChatMessage] = relationship(back_populates="versions")


class Attachment(TimestampMixin, Base):
    """A file from the chat or from a report form. local_path is the on-disk copy."""

    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    message_pk: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    report_id: Mapped[int | None] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # photo, document, video, voice, ...
    file_id: Mapped[str] = mapped_column(String(256))
    file_unique_id: Mapped[str] = mapped_column(String(128), index=True)
    file_name: Mapped[str | None] = mapped_column(String(512))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    local_path: Mapped[str | None] = mapped_column(String(1024))
    # pending | done | too_big | failed
    status: Mapped[str] = mapped_column(String(16), default="pending")
    # File replaced by a message edit — kept in history, absent from the current message state.
    replaced_by_edit: Mapped[bool | None] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------- reports


class Report(TimestampMixin, Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(6), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)  # bug | feature
    status: Mapped[str] = mapped_column(String(32), default="open")
    status_comment: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str | None] = mapped_column(String(512))
    # Form fields: description/steps/actual/expected/environment/logs or idea/problem/implementation
    fields: Mapped[dict[str, Any]]
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    post_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    post_thread_id: Mapped[int | None] = mapped_column(BigInteger)
    post_message_id: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), onupdate=utcnow
    )

    attachments: Mapped[list[Attachment]] = relationship(order_by="Attachment.id")


class ChangelogEntry(TimestampMixin, Base):
    __tablename__ = "changelog"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version: Mapped[str | None] = mapped_column(String(64))
    text_html: Mapped[str] = mapped_column(Text)
    author_id: Mapped[int] = mapped_column(BigInteger)
    post_message_id: Mapped[int | None] = mapped_column(BigInteger)


# ---------------------------------------------------------------- moderation


class Violation(TimestampMixin, Base):
    __tablename__ = "violations"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    thread_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    severity: Mapped[str] = mapped_column(String(8))  # soft | hard
    findings: Mapped[list[Any]]
    text: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(32))  # logged | deleted | muted


# ---------------------------------------------------------------- admin settings and drafts


class AppSetting(Base):
    """Runtime settings changed from the admin menu: notification toggles, form quick-button presets.
    Generic key/value with an arbitrary JSON value."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict[str, Any]]
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), onupdate=utcnow
    )


class Draft(TimestampMixin, Base):
    """An unfinished form (bug/feature): one row per user, updated step by step.
    Lets an admin see who started a form but never finished it."""

    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str | None] = mapped_column(String(512))
    kind: Mapped[str] = mapped_column(String(16))  # bug | feature
    fields: Mapped[dict[str, Any]]
    files_count: Mapped[int] = mapped_column(Integer, default=0)
    step: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | submitted | cancelled
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), onupdate=utcnow
    )
