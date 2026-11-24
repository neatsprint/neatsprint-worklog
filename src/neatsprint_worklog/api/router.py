"""Worklog API: timer, manual time, sync, timesheet and export."""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from neatsprint_lib.auth import Principal, get_principal, require_permission
from neatsprint_lib.db import get_tenant_session
from neatsprint_lib.errors import ApiError
from neatsprint_lib.events import enqueue
from neatsprint_lib.ids import new_id
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from neatsprint_worklog.api.schemas import (
    EntryUpdate,
    IdleReport,
    ManualEntry,
    SessionStart,
    SessionStop,
    SyncRequest,
)
from neatsprint_worklog.db.models import IdleSegment, TimeEntry, TimeEntryAudit, WorkSession
from neatsprint_worklog.services.storage import presign_get, upload_csv

router = APIRouter(tags=["work"])


@router.get("/ping", tags=["platform"], summary="Service liveness echo")
async def ping() -> dict[str, str]:
    return {"ping": "pong", "service": "worklog"}

SessionDep = Annotated[AsyncSession, Depends(get_tenant_session)]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
TimerPerm = Annotated[Principal, Depends(require_permission("work:timer:start"))]
WritePerm = Annotated[Principal, Depends(require_permission("work:log:write"))]
ViewPerm = Annotated[Principal, Depends(require_permission("work:log:view_own"))]
ExportPerm = Annotated[Principal, Depends(require_permission("report:export"))]

EDIT_WINDOW = timedelta(hours=24)
CLOCK_TOLERANCE_SECONDS = 120


def envelope(request: Request, data: object, **meta: object) -> dict[str, object]:
    return {
        "data": data,
        "meta": {"request_id": getattr(request.state, "request_id", None), **meta},
    }


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _live_seconds(session: WorkSession, now: datetime) -> float:
    if session.state == "STOPPED":
        return max(0.0, session.monotonic_elapsed_seconds)
    elapsed = (now - session.started_at).total_seconds() - session.paused_seconds
    if session.state == "PAUSED" and session.last_heartbeat_at is not None:
        elapsed -= max(0.0, (now - session.last_heartbeat_at).total_seconds())
    return max(0.0, round(elapsed, 1))


def session_payload(session: WorkSession, now: datetime | None = None) -> dict[str, object]:
    moment = now or _now()
    return {
        "id": str(session.id),
        "user_id": str(session.user_id),
        "item_id": str(session.item_id) if session.item_id else None,
        "project_id": str(session.project_id) if session.project_id else None,
        "device_id": session.device_id,
        "state": session.state,
        "started_at": session.started_at.isoformat(),
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
        "elapsed_seconds": _live_seconds(session, moment),
        "comment": session.comment,
    }


def entry_payload(entry: TimeEntry) -> dict[str, object]:
    return {
        "id": str(entry.id),
        "session_id": str(entry.session_id) if entry.session_id else None,
        "user_id": str(entry.user_id),
        "item_id": str(entry.item_id) if entry.item_id else None,
        "project_id": str(entry.project_id) if entry.project_id else None,
        "started_at": entry.started_at.isoformat(),
        "ended_at": entry.ended_at.isoformat(),
        "duration_seconds": entry.duration_seconds,
        "source": entry.source,
        "comment": entry.comment,
        "client_id": str(entry.client_id),
    }


async def _open_session(
    session: AsyncSession, tenant_id: UUID, user_id: UUID
) -> WorkSession | None:
    return (
        await session.execute(
            select(WorkSession).where(
                WorkSession.tenant_id == tenant_id,
                WorkSession.user_id == user_id,
                WorkSession.state.in_(("RUNNING", "PAUSED")),
            )
        )
    ).scalar_one_or_none()


async def _task_item(request: Request, tenant_id: UUID, item_id: UUID) -> dict[str, Any]:
    settings = request.app.state.settings
    token = settings.service_token or settings.bootstrap_token
    if not token:
        raise ApiError(503, "dependency_unavailable", "Task validation is not configured.")
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(
                f"{settings.task_internal_url}/internal/items/{item_id}",
                params={"tenant_id": str(tenant_id)},
                headers={"X-Service-Token": token},
            )
    except httpx.HTTPError as exc:
        raise ApiError(
            503, "dependency_unavailable", "Could not validate the task right now."
        ) from exc
    if response.status_code == 404:
        raise ApiError(422, "invalid_item", "That task does not exist in this workspace.")
    if response.status_code != 200:
        raise ApiError(
            503, "dependency_unavailable", "Could not validate the task right now."
        )
    payload = response.json().get("data")
    if not isinstance(payload, dict):
        raise ApiError(503, "dependency_unavailable", "Could not validate the task right now.")
    return payload


async def _resolve_scope(
    request: Request,
    tenant_id: UUID,
    item_id: UUID | None,
    project_id: UUID | None,
) -> UUID | None:
    if item_id is None:
        return project_id
    item = await _task_item(request, tenant_id, item_id)
    resolved = UUID(str(item["project_id"]))
    if project_id is not None and resolved != project_id:
        raise ApiError(422, "invalid_item", "The task does not belong to that project.")
    return resolved


def _audit(
    session: AsyncSession,
    *,
    entry: TimeEntry,
    actor_id: UUID,
    action: str,
    before: dict[str, object] | None,
    after: dict[str, object] | None,
) -> None:
    session.add(
        TimeEntryAudit(
            tenant_id=entry.tenant_id,
            entry_id=entry.id,
            actor_id=actor_id,
            action=action,
            before=before,
            after=after,
        )
    )


def _assert_edit_window(principal: Principal, entry: TimeEntry) -> None:
    if principal.can("work:log:edit_any"):
        return
    if entry.user_id != principal.user_id:
        raise ApiError(403, "permission_denied", "You can only edit your own time.")
    if _now() - entry.created_at > EDIT_WINDOW:
        raise ApiError(
            403,
            "window_closed",
            "Time entries can only be edited within 24 hours.",
        )


def _entry_duration(
    started_at: datetime, ended_at: datetime, requested: int | None
) -> int:
    span = int((ended_at - started_at).total_seconds())
    if span <= 0:
        raise ApiError(422, "validation_failed", "The end time must follow the start time.")
    duration = requested if requested is not None else span
    if duration <= 0 or duration > span + CLOCK_TOLERANCE_SECONDS:
        raise ApiError(422, "validation_failed", "The duration does not match the time range.")
    return duration


def _add_entry(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    item_id: UUID | None,
    project_id: UUID | None,
    started_at: datetime,
    ended_at: datetime,
    duration_seconds: int,
    source: str,
    comment: str | None,
    client_id: UUID,
    session_id: UUID | None = None,
) -> TimeEntry:
    entry = TimeEntry(
        id=new_id(),
        tenant_id=tenant_id,
        session_id=session_id,
        user_id=user_id,
        item_id=item_id,
        project_id=project_id,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=duration_seconds,
        source=source,
        comment=comment,
        client_id=client_id,
    )
    session.add(entry)
    enqueue(
        session,
        event_type="time_entry.recorded",
        tenant_id=tenant_id,
        actor_id=user_id,
        payload={
            "entry_id": str(entry.id),
            "client_id": str(client_id),
            "user_id": str(user_id),
            "item_id": str(item_id) if item_id else None,
            "project_id": str(project_id) if project_id else None,
            "duration_seconds": duration_seconds,
            "source": source,
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
        },
    )
    return entry


async def _existing_entry(
    session: AsyncSession, tenant_id: UUID, client_id: UUID
) -> TimeEntry | None:
    return (
        await session.execute(
            select(TimeEntry).where(
                TimeEntry.tenant_id == tenant_id,
                TimeEntry.client_id == client_id,
                TimeEntry.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


@router.post("/start", status_code=201, summary="Start a work session")
async def start_session(
    payload: SessionStart, principal: TimerPerm, request: Request, session: SessionDep
) -> JSONResponse:
    existing = await _open_session(session, principal.tenant_id, principal.user_id)
    if existing is not None:
        raise ApiError(
            409,
            "session_exists",
            "A work session is already running. Stop it before starting another.",
        )
    project_id = await _resolve_scope(
        request, principal.tenant_id, payload.item_id, payload.project_id
    )
    started = _now()
    work_session = WorkSession(
        id=new_id(),
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        item_id=payload.item_id,
        project_id=project_id,
        device_id=payload.device_id,
        state="RUNNING",
        started_at=started,
        last_heartbeat_at=started,
        comment=payload.comment,
    )
    session.add(work_session)
    enqueue(
        session,
        event_type="work.session_started",
        tenant_id=principal.tenant_id,
        actor_id=principal.user_id,
        payload={
            "session_id": str(work_session.id),
            "item_id": str(payload.item_id) if payload.item_id else None,
            "project_id": str(project_id) if project_id else None,
        },
    )
    await session.commit()
    return JSONResponse(
        envelope(request, session_payload(work_session, started)), status_code=201
    )


@router.post("/stop", summary="Stop the current work session")
async def stop_session(
    payload: SessionStop, principal: TimerPerm, request: Request, session: SessionDep
) -> JSONResponse:
    work_session = await _open_session(session, principal.tenant_id, principal.user_id)
    if work_session is None:
        raise ApiError(404, "no_active_session", "There is no running work session.")
    now = _now()
    live = _live_seconds(work_session, now)
    requested = payload.elapsed_seconds if payload.elapsed_seconds is not None else live
    requested = max(0.0, float(requested))
    duration = int(min(requested, live + CLOCK_TOLERANCE_SECONDS))
    skew = int((requested - live) * 1000)

    work_session.state = "STOPPED"
    work_session.ended_at = now
    work_session.monotonic_elapsed_seconds = float(duration)
    work_session.client_clock_skew_ms = skew
    client_id = payload.client_id or new_id()
    entry = await _existing_entry(session, principal.tenant_id, client_id)
    if entry is None and duration > 0:
        entry = _add_entry(
            session,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            item_id=work_session.item_id,
            project_id=work_session.project_id,
            started_at=work_session.started_at,
            ended_at=now,
            duration_seconds=duration,
            source="TIMER",
            comment=work_session.comment,
            client_id=client_id,
            session_id=work_session.id,
        )
    enqueue(
        session,
        event_type="work.session_stopped",
        tenant_id=principal.tenant_id,
        actor_id=principal.user_id,
        payload={
            "session_id": str(work_session.id),
            "duration_seconds": duration,
            "client_clock_skew_ms": skew,
        },
    )
    await session.commit()
    return JSONResponse(
        envelope(
            request,
            {
                "session": session_payload(work_session, now),
                "entry": entry_payload(entry) if entry else None,
            },
        )
    )


@router.post("/pause", summary="Pause the current session")
async def pause_session(
    principal: TimerPerm, request: Request, session: SessionDep
) -> JSONResponse:
    work_session = await _open_session(session, principal.tenant_id, principal.user_id)
    if work_session is None or work_session.state != "RUNNING":
        raise ApiError(409, "not_running", "There is no running work session.")
    work_session.state = "PAUSED"
    work_session.last_heartbeat_at = _now()
    await session.commit()
    return JSONResponse(envelope(request, session_payload(work_session)))


@router.post("/resume", summary="Resume a paused session")
async def resume_session(
    principal: TimerPerm, request: Request, session: SessionDep
) -> JSONResponse:
    work_session = await _open_session(session, principal.tenant_id, principal.user_id)
    if work_session is None or work_session.state != "PAUSED":
        raise ApiError(409, "not_paused", "There is no paused work session.")
    now = _now()
    if work_session.last_heartbeat_at is not None:
        work_session.paused_seconds += max(
            0.0, (now - work_session.last_heartbeat_at).total_seconds()
        )
    work_session.state = "RUNNING"
    work_session.last_heartbeat_at = now
    await session.commit()
    return JSONResponse(envelope(request, session_payload(work_session, now)))


@router.get("/current", summary="Current work session")
async def current_session(
    principal: CurrentPrincipal, request: Request, session: SessionDep
) -> JSONResponse:
    work_session = await _open_session(session, principal.tenant_id, principal.user_id)
    return JSONResponse(
        envelope(request, session_payload(work_session) if work_session else None)
    )


@router.post("/idle", summary="Record an idle segment")
async def record_idle(
    payload: IdleReport, principal: TimerPerm, request: Request, session: SessionDep
) -> JSONResponse:
    work_session = await session.get(WorkSession, payload.session_id)
    if (
        work_session is None
        or work_session.tenant_id != principal.tenant_id
        or work_session.user_id != principal.user_id
    ):
        raise ApiError(404, "not_found", "Work session not found.")
    session.add(
        IdleSegment(
            tenant_id=principal.tenant_id,
            session_id=work_session.id,
            started_at=payload.started_at,
            ended_at=payload.ended_at,
            idle_seconds=payload.idle_seconds,
            resolution=payload.resolution,
        )
    )
    if payload.resolution in ("ACCEPTED", "TRUNCATED") and work_session.state != "STOPPED":
        live = _live_seconds(work_session, _now())
        work_session.paused_seconds += min(float(payload.idle_seconds), live)
    await session.commit()
    return JSONResponse(envelope(request, session_payload(work_session)))


@router.post("/sync", summary="Batch sync offline time entries")
async def sync_entries(
    payload: SyncRequest, principal: WritePerm, request: Request, session: SessionDep
) -> JSONResponse:
    created: list[str] = []
    skipped: list[str] = []
    scopes: dict[UUID | None, UUID | None] = {}
    for item in payload.entries:
        if await _existing_entry(session, principal.tenant_id, item.client_id) is not None:
            skipped.append(str(item.client_id))
            continue
        if item.item_id not in scopes:
            scopes[item.item_id] = await _resolve_scope(
                request, principal.tenant_id, item.item_id, item.project_id
            )
        project_id = scopes[item.item_id]
        duration = _entry_duration(item.started_at, item.ended_at, item.duration_seconds)
        entry = _add_entry(
            session,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            item_id=item.item_id,
            project_id=project_id,
            started_at=item.started_at,
            ended_at=item.ended_at,
            duration_seconds=duration,
            source=item.source,
            comment=item.comment,
            client_id=item.client_id,
        )
        created.append(str(entry.id))
    await session.commit()
    return JSONResponse(
        envelope(request, {"created": created, "skipped": skipped}, count=len(created))
    )


@router.post("/entries", status_code=201, summary="Add manual time")
async def create_entry(
    payload: ManualEntry, principal: WritePerm, request: Request, session: SessionDep
) -> JSONResponse:
    project_id = await _resolve_scope(
        request, principal.tenant_id, payload.item_id, payload.project_id
    )
    duration = _entry_duration(
        payload.started_at, payload.ended_at, payload.duration_seconds
    )
    entry = _add_entry(
        session,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        item_id=payload.item_id,
        project_id=project_id,
        started_at=payload.started_at,
        ended_at=payload.ended_at,
        duration_seconds=duration,
        source="MANUAL",
        comment=payload.comment,
        client_id=payload.client_id or new_id(),
    )
    _audit(
        session,
        entry=entry,
        actor_id=principal.user_id,
        action="CREATE",
        before=None,
        after=entry_payload(entry),
    )
    await session.commit()
    return JSONResponse(envelope(request, entry_payload(entry)), status_code=201)


@router.get("/entries", summary="List my time entries")
async def list_entries(
    principal: ViewPerm,
    request: Request,
    session: SessionDep,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    user_id: UUID | None = Query(default=None),
) -> JSONResponse:
    start, end = _range(from_date, to_date)
    target_user = _target_user(principal, user_id)
    rows = (
        await session.execute(
            select(TimeEntry)
            .where(
                TimeEntry.tenant_id == principal.tenant_id,
                TimeEntry.user_id == target_user,
                TimeEntry.deleted_at.is_(None),
                TimeEntry.started_at >= start,
                TimeEntry.started_at < end,
            )
            .order_by(TimeEntry.started_at)
        )
    ).scalars()
    return JSONResponse(envelope(request, [entry_payload(row) for row in rows]))


@router.patch("/entries/{entry_id}", summary="Edit a time entry")
async def update_entry(
    entry_id: UUID,
    payload: EntryUpdate,
    principal: WritePerm,
    request: Request,
    session: SessionDep,
) -> JSONResponse:
    entry = await session.get(TimeEntry, entry_id)
    if (
        entry is None
        or entry.tenant_id != principal.tenant_id
        or entry.deleted_at is not None
    ):
        raise ApiError(404, "not_found", "Time entry not found.")
    _assert_edit_window(principal, entry)
    before = entry_payload(entry)
    data = payload.model_dump(exclude_unset=True)
    if "item_id" in data:
        entry.item_id = data["item_id"]
        entry.project_id = await _resolve_scope(
            request, principal.tenant_id, data["item_id"], data.get("project_id")
        )
    elif "project_id" in data:
        entry.project_id = data["project_id"]
    if "started_at" in data:
        entry.started_at = data["started_at"]
    if "ended_at" in data:
        entry.ended_at = data["ended_at"]
    if "comment" in data:
        entry.comment = data["comment"]
    entry.duration_seconds = _entry_duration(
        entry.started_at, entry.ended_at, data.get("duration_seconds")
    )
    _audit(
        session,
        entry=entry,
        actor_id=principal.user_id,
        action="UPDATE",
        before=before,
        after=entry_payload(entry),
    )
    enqueue(
        session,
        event_type="time_entry.updated",
        tenant_id=principal.tenant_id,
        actor_id=principal.user_id,
        payload={
            "entry_id": str(entry.id),
            "user_id": str(entry.user_id),
            "project_id": str(entry.project_id) if entry.project_id else None,
            "item_id": str(entry.item_id) if entry.item_id else None,
            "started_at": entry.started_at.isoformat(),
            "duration_seconds": entry.duration_seconds,
            "source": entry.source,
        },
    )
    await session.commit()
    return JSONResponse(envelope(request, entry_payload(entry)))


@router.delete("/entries/{entry_id}", status_code=204, summary="Delete a time entry")
async def delete_entry(
    entry_id: UUID, principal: WritePerm, session: SessionDep
) -> Response:
    entry = await session.get(TimeEntry, entry_id)
    if (
        entry is None
        or entry.tenant_id != principal.tenant_id
        or entry.deleted_at is not None
    ):
        raise ApiError(404, "not_found", "Time entry not found.")
    _assert_edit_window(principal, entry)
    entry.deleted_at = _now()
    _audit(
        session,
        entry=entry,
        actor_id=principal.user_id,
        action="DELETE",
        before=entry_payload(entry),
        after=None,
    )
    enqueue(
        session,
        event_type="time_entry.deleted",
        tenant_id=principal.tenant_id,
        actor_id=principal.user_id,
        payload={"entry_id": str(entry.id), "user_id": str(entry.user_id)},
    )
    await session.commit()
    return Response(status_code=204)


@router.get("/timesheet", summary="Weekly timesheet")
async def timesheet(
    principal: ViewPerm,
    request: Request,
    session: SessionDep,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    user_id: UUID | None = Query(default=None),
) -> JSONResponse:
    start, end = _range(from_date, to_date)
    target_user = _target_user(principal, user_id)
    rows = list(
        (
            await session.execute(
                select(TimeEntry)
                .where(
                    TimeEntry.tenant_id == principal.tenant_id,
                    TimeEntry.user_id == target_user,
                    TimeEntry.deleted_at.is_(None),
                    TimeEntry.started_at >= start,
                    TimeEntry.started_at < end,
                )
                .order_by(TimeEntry.started_at)
            )
        ).scalars()
    )
    by_day: dict[str, int] = defaultdict(int)
    by_project: dict[str, int] = defaultdict(int)
    by_item: dict[str, int] = defaultdict(int)
    for entry in rows:
        by_day[entry.started_at.date().isoformat()] += entry.duration_seconds
        if entry.project_id:
            by_project[str(entry.project_id)] += entry.duration_seconds
        if entry.item_id:
            by_item[str(entry.item_id)] += entry.duration_seconds
    return JSONResponse(
        envelope(
            request,
            {
                "from": start.date().isoformat(),
                "to": (end - timedelta(days=1)).date().isoformat(),
                "user_id": str(target_user),
                "total_seconds": sum(by_day.values()),
                "days": [
                    {"date": day, "seconds": seconds}
                    for day, seconds in sorted(by_day.items())
                ],
                "projects": [
                    {"project_id": project, "seconds": seconds}
                    for project, seconds in sorted(by_project.items())
                ],
                "items": [
                    {"item_id": item, "seconds": seconds}
                    for item, seconds in sorted(by_item.items())
                ],
                "entries": [entry_payload(entry) for entry in rows],
            },
        )
    )


@router.get("/export", summary="Export a timesheet CSV")
async def export_timesheet(
    principal: ExportPerm,
    request: Request,
    session: SessionDep,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    user_id: UUID | None = Query(default=None),
) -> JSONResponse:
    start, end = _range(from_date, to_date)
    target_user = _target_user(principal, user_id)
    rows = list(
        (
            await session.execute(
                select(TimeEntry)
                .where(
                    TimeEntry.tenant_id == principal.tenant_id,
                    TimeEntry.user_id == target_user,
                    TimeEntry.deleted_at.is_(None),
                    TimeEntry.started_at >= start,
                    TimeEntry.started_at < end,
                )
                .order_by(TimeEntry.started_at)
            )
        ).scalars()
    )
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "started_at",
            "ended_at",
            "duration_seconds",
            "source",
            "project_id",
            "item_id",
            "comment",
            "client_id",
        ]
    )
    for entry in rows:
        writer.writerow(
            [
                entry.started_at.isoformat(),
                entry.ended_at.isoformat(),
                entry.duration_seconds,
                entry.source,
                str(entry.project_id) if entry.project_id else "",
                str(entry.item_id) if entry.item_id else "",
                entry.comment or "",
                str(entry.client_id),
            ]
        )
    key = upload_csv(request, str(principal.tenant_id), buffer.getvalue().encode())
    return JSONResponse(
        envelope(
            request,
            {"download_url": presign_get(request, key), "expires_in": 300, "rows": len(rows)},
        )
    )


def _range(from_date: date | None, to_date: date | None) -> tuple[datetime, datetime]:
    end_day = to_date or _now().date()
    start_day = from_date or (end_day - timedelta(days=6))
    if start_day > end_day:
        raise ApiError(422, "validation_failed", "The start date must precede the end date.")
    start = datetime.combine(start_day, time.min, tzinfo=UTC)
    end = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=UTC)
    return start, end


def _target_user(principal: Principal, user_id: UUID | None) -> UUID:
    if user_id is None or user_id == principal.user_id:
        return principal.user_id
    if not principal.can("work:log:view_all"):
        raise ApiError(403, "permission_denied", "You cannot view another user's time.")
    return user_id
