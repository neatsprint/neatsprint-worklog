"""Internal Worklog lookups for the Monitoring service."""

from __future__ import annotations

import secrets
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from neatsprint_lib.db import bind_tenant, get_session
from neatsprint_lib.errors import ApiError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from neatsprint_worklog.db.models import TimeEntry, WorkSession

router = APIRouter(prefix="/internal", tags=["internal"])
AdminSession = Annotated[AsyncSession, Depends(get_session)]


async def _service_guard(
    request: Request, x_service_token: Annotated[str | None, Header()] = None
) -> None:
    expected = request.app.state.settings.bootstrap_token
    if not expected or not x_service_token or not secrets.compare_digest(
        x_service_token, expected
    ):
        raise ApiError(403, "permission_denied", "Invalid service token.")


ServiceDep = Annotated[None, Depends(_service_guard)]


def envelope(request: Request, data: object) -> dict[str, object]:
    return {
        "data": data,
        "meta": {"request_id": getattr(request.state, "request_id", None)},
    }


@router.get("/sessions/{session_id}", summary="Internal work session lookup")
async def internal_session(
    session_id: UUID,
    tenant_id: UUID,
    _: ServiceDep,
    request: Request,
    session: AdminSession,
) -> JSONResponse:
    bind_tenant(session, tenant_id)
    work_session = await session.get(WorkSession, session_id)
    if work_session is None or work_session.tenant_id != tenant_id:
        raise ApiError(404, "not_found", "Work session not found.")
    return JSONResponse(
        envelope(
            request,
            {
                "id": str(work_session.id),
                "user_id": str(work_session.user_id),
                "item_id": str(work_session.item_id) if work_session.item_id else None,
                "project_id": (
                    str(work_session.project_id) if work_session.project_id else None
                ),
                "state": work_session.state,
                "started_at": work_session.started_at.isoformat(),
                "ended_at": (
                    work_session.ended_at.isoformat() if work_session.ended_at else None
                ),
            },
        )
    )


@router.get("/entries", summary="Internal paginated time entries")
async def internal_entries(
    tenant_id: UUID,
    _: ServiceDep,
    request: Request,
    session: AdminSession,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> JSONResponse:
    bind_tenant(session, tenant_id)
    rows = (
        await session.execute(
            select(TimeEntry)
            .where(
                TimeEntry.tenant_id == tenant_id,
                TimeEntry.deleted_at.is_(None),
            )
            .order_by(TimeEntry.started_at, TimeEntry.id)
            .offset(offset)
            .limit(limit)
        )
    ).scalars()
    return JSONResponse(
        envelope(
            request,
            [
                {
                    "entry_id": str(row.id),
                    "user_id": str(row.user_id),
                    "project_id": str(row.project_id) if row.project_id else None,
                    "item_id": str(row.item_id) if row.item_id else None,
                    "started_at": row.started_at.isoformat(),
                    "duration_seconds": row.duration_seconds,
                    "source": row.source,
                }
                for row in rows
            ],
        )
    )
