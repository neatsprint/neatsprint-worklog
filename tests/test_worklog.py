from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from neatsprint_lib.auth import Principal
from neatsprint_lib.errors import ApiError

from neatsprint_worklog.api import router as worklog_router
from neatsprint_worklog.api.router import (
    _entry_duration,
    _live_seconds,
    _range,
    _resolve_assignment,
    _target_user,
)
from neatsprint_worklog.db.models import WorkSession


def _session(**overrides: object) -> WorkSession:
    values: dict[str, object] = {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "user_id": uuid4(),
        "state": "RUNNING",
        "started_at": datetime(2026, 11, 24, 9, 0, tzinfo=UTC),
        "paused_seconds": 0.0,
        "monotonic_elapsed_seconds": 0.0,
        "client_clock_skew_ms": 0,
    }
    values.update(overrides)
    return WorkSession(**values)


def _principal(user_id: object, permissions: set[str]) -> Principal:
    return Principal(
        user_id=user_id,
        tenant_id=uuid4(),
        session_id=None,
        roles=("MEMBER",),
        permissions=frozenset(permissions),
        jti="test",
    )


def test_live_seconds_counts_wall_time_and_pauses() -> None:
    now = datetime(2026, 11, 24, 9, 10, tzinfo=UTC)
    session = _session()

    assert _live_seconds(session, now) == 600.0

    session.paused_seconds = 60.0
    assert _live_seconds(session, now) == 540.0

    session.state = "PAUSED"
    session.last_heartbeat_at = now - timedelta(seconds=30)
    assert _live_seconds(session, now) == 510.0


def test_stopped_session_reports_recorded_elapsed() -> None:
    session = _session(state="STOPPED", monotonic_elapsed_seconds=42.5)
    assert _live_seconds(session, datetime(2026, 11, 24, 12, 0, tzinfo=UTC)) == 42.5


def test_entry_duration_rejects_clock_gaming() -> None:
    started = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)
    ended = datetime(2026, 11, 24, 9, 10, tzinfo=UTC)

    assert _entry_duration(started, ended, None) == 600
    with pytest.raises(ApiError):
        _entry_duration(started, ended, 3600)
    with pytest.raises(ApiError):
        _entry_duration(ended, started, 10)


def test_range_defaults_to_seven_days() -> None:
    start, end = _range(None, None)
    assert (end - start) == timedelta(days=7)


def _stub_assignment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    items: dict[object, dict[str, object]],
    sprints: dict[object, dict[str, object]],
    allow_without_sprint: bool,
) -> None:
    async def task_item(request: object, tenant_id: object, item_id: object) -> dict[str, object]:
        if item_id not in items:
            raise ApiError(422, "invalid_item", "missing")
        return items[item_id]

    async def task_sprint(
        request: object, tenant_id: object, sprint_id: object
    ) -> dict[str, object]:
        if sprint_id not in sprints:
            raise ApiError(422, "invalid_sprint", "missing")
        return sprints[sprint_id]

    async def time_policy(request: object, tenant_id: object) -> bool:
        return allow_without_sprint

    monkeypatch.setattr(worklog_router, "_task_item", task_item)
    monkeypatch.setattr(worklog_router, "_task_sprint", task_sprint)
    monkeypatch.setattr(worklog_router, "_time_policy", time_policy)


async def test_item_in_sprint_inherits_sprint_when_none_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, sprint, item = uuid4(), uuid4(), uuid4()
    _stub_assignment(
        monkeypatch,
        items={item: {"project_id": str(project), "sprint_id": str(sprint)}},
        sprints={sprint: {"project_id": str(project)}},
        allow_without_sprint=False,
    )

    assert await _resolve_assignment(None, uuid4(), item, None, None) == (project, sprint)  # type: ignore[arg-type]


async def test_selected_sprint_must_contain_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    project, sprint, other_sprint, item = uuid4(), uuid4(), uuid4(), uuid4()
    _stub_assignment(
        monkeypatch,
        items={item: {"project_id": str(project), "sprint_id": str(other_sprint)}},
        sprints={sprint: {"project_id": str(project)}, other_sprint: {"project_id": str(project)}},
        allow_without_sprint=True,
    )

    with pytest.raises(ApiError) as excinfo:
        await _resolve_assignment(None, uuid4(), item, None, sprint)  # type: ignore[arg-type]
    assert excinfo.value.code == "invalid_item"


async def test_sprint_is_required_when_setting_disallows_no_sprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, item = uuid4(), uuid4()
    _stub_assignment(
        monkeypatch,
        items={item: {"project_id": str(project), "sprint_id": None}},
        sprints={},
        allow_without_sprint=False,
    )

    with pytest.raises(ApiError) as excinfo:
        await _resolve_assignment(None, uuid4(), item, None, None)  # type: ignore[arg-type]
    assert excinfo.value.code == "sprint_required"


async def test_time_without_sprint_allowed_when_setting_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_assignment(monkeypatch, items={}, sprints={}, allow_without_sprint=True)

    assert await _resolve_assignment(None, uuid4(), None, None, None) == (None, None)  # type: ignore[arg-type]


async def test_sprint_must_belong_to_the_project(monkeypatch: pytest.MonkeyPatch) -> None:
    project, other_project, sprint = uuid4(), uuid4(), uuid4()
    _stub_assignment(
        monkeypatch,
        items={},
        sprints={sprint: {"project_id": str(other_project)}},
        allow_without_sprint=True,
    )

    with pytest.raises(ApiError) as excinfo:
        await _resolve_assignment(None, uuid4(), None, project, sprint)  # type: ignore[arg-type]
    assert excinfo.value.code == "invalid_sprint"


def test_target_user_requires_view_all_for_other_users() -> None:
    me = uuid4()
    other = uuid4()
    own = _principal(me, {"work:log:view_own"})
    all_view = _principal(me, {"work:log:view_own", "work:log:view_all"})

    assert _target_user(own, None) == me
    assert _target_user(own, me) == me
    with pytest.raises(ApiError):
        _target_user(own, other)
    assert _target_user(all_view, other) == other
