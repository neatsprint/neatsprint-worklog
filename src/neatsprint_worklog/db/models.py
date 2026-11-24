"""Worklog models: sessions, time entries, idle segments and audit."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from neatsprint_lib.db import Base, SoftDeleteMixin, TenantMixin, TimestampMixin
from neatsprint_lib.events import OutboxEvent as OutboxEvent  # noqa: F401 - register table
from neatsprint_lib.ids import new_id
from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

SESSION_STATES = ("RUNNING", "PAUSED", "STOPPED")
ENTRY_SOURCES = ("TIMER", "MANUAL", "IMPORT")
IDLE_RESOLUTIONS = ("ACCEPTED", "DISCARDED", "TRUNCATED")


class WorkSession(Base, TenantMixin, TimestampMixin):
    __tablename__ = "work_sessions"
    __table_args__ = (
        Index("ix_sessions_user_started", "tenant_id", "user_id", "started_at"),
        Index(
            "ux_open_session_per_user",
            "tenant_id",
            "user_id",
            unique=True,
            postgresql_where=text("state IN ('RUNNING','PAUSED')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=new_id
    )
    user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    item_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    project_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    device_id: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="RUNNING")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    monotonic_elapsed_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    paused_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    client_clock_skew_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    comment: Mapped[str | None] = mapped_column(Text)


class TimeEntry(Base, TenantMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "time_entries"
    __table_args__ = (
        Index("ix_entries_user_started", "tenant_id", "user_id", "started_at"),
        Index("ix_entries_project", "tenant_id", "project_id", "started_at"),
        Index("ix_entries_client", "tenant_id", "client_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=new_id
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("work_sessions.id", ondelete="SET NULL")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    item_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    project_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="TIMER")
    comment: Mapped[str | None] = mapped_column(Text)
    client_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)


class IdleSegment(Base, TenantMixin):
    __tablename__ = "idle_segments"
    __table_args__ = (Index("ix_idle_session", "session_id", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=new_id
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("work_sessions.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idle_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution: Mapped[str] = mapped_column(String(16), nullable=False)


class TimeEntryAudit(Base, TenantMixin):
    __tablename__ = "time_entry_audit"
    __table_args__ = (Index("ix_entry_audit_entry", "entry_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=new_id
    )
    entry_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
