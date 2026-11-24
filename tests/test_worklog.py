from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from neatsprint_lib.auth import Principal
from neatsprint_lib.errors import ApiError

from neatsprint_worklog.api.router import _entry_duration, _live_seconds, _range, _target_user
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
