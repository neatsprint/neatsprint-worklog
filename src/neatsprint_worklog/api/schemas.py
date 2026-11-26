from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class SessionStart(BaseModel):
    item_id: UUID | None = None
    project_id: UUID | None = None
    sprint_id: UUID | None = None
    device_id: str | None = Field(default=None, max_length=128)
    comment: str | None = Field(default=None, max_length=2000)


class SessionStop(BaseModel):
    elapsed_seconds: float | None = Field(default=None, ge=0)
    client_id: UUID | None = None


class IdleReport(BaseModel):
    session_id: UUID
    started_at: datetime
    ended_at: datetime
    idle_seconds: int = Field(ge=0)
    resolution: str = Field(pattern="^(ACCEPTED|DISCARDED|TRUNCATED)$")

    @model_validator(mode="after")
    def _ordered(self) -> IdleReport:
        if self.ended_at < self.started_at:
            raise ValueError("ended_at must be after started_at")
        return self


class ManualEntry(BaseModel):
    item_id: UUID | None = None
    project_id: UUID | None = None
    sprint_id: UUID | None = None
    started_at: datetime
    ended_at: datetime
    duration_seconds: int | None = Field(default=None, ge=1)
    comment: str | None = Field(default=None, max_length=2000)
    client_id: UUID | None = None

    @model_validator(mode="after")
    def _ordered(self) -> ManualEntry:
        if self.ended_at <= self.started_at:
            raise ValueError("ended_at must be after started_at")
        return self


class EntryUpdate(BaseModel):
    item_id: UUID | None = None
    project_id: UUID | None = None
    sprint_id: UUID | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: int | None = Field(default=None, ge=1)
    comment: str | None = Field(default=None, max_length=2000)


class SyncEntry(BaseModel):
    client_id: UUID
    item_id: UUID | None = None
    project_id: UUID | None = None
    sprint_id: UUID | None = None
    started_at: datetime
    ended_at: datetime
    duration_seconds: int = Field(ge=1)
    source: str = Field(default="IMPORT", pattern="^(TIMER|MANUAL|IMPORT)$")
    comment: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _ordered(self) -> SyncEntry:
        if self.ended_at <= self.started_at:
            raise ValueError("ended_at must be after started_at")
        return self


class SyncRequest(BaseModel):
    entries: list[SyncEntry] = Field(min_length=1, max_length=500)
