# neatsprint-worklog

NeatSprint **Worklog Service**.

| Item | Value |
| :--- | :--- |
| Service name | `worklog` |
| Container port | `8000` |
| Host port (dev) | `8005` |
| Gateway prefix | `/api/v1/work` |
| Database | `db_worklog` |
| Owner | OpenCade (AI) / Laksiri (approval) |

## Responsibilities

See `../../docs/ARCHITECTURE.md` §3.3 for the authoritative boundary. This service owns its
logical database and must never read another service's tables.

## Run locally

```bash
uv sync
cp .env.example .env
uv run uvicorn neatsprint_worklog.main:app --reload --port 8000
```

Or from the orchestrator: `docker compose up -d neatsprint-worklog`.

## Endpoints

| Method | Path | Purpose |
| :--- | :--- | :--- |
| GET | `/healthz` | Liveness (no dependencies) |
| GET | `/readyz` | Readiness (database check) |
| GET | `/metrics` | Prometheus metrics |
| GET | `/api/v1/ping` | Skeleton echo |

Domain endpoints are added per `docs/DEVELOPMENT_PLAN.md`.

## Test

```bash
uv run pytest
uv run ruff check .
uv run mypy src
```

## Standards

- Tenant scoping: every tenant-owned query filters `tenant_id` (docs/SECURITY.md).
- Errors: raise `neatsprint_lib.errors.ApiError`; never hand-build error JSON.
- Logs: JSON via `neatsprint_lib.logging`; no PII, tokens or screenshot content.
- Contracts: publish OpenAPI to `neatsprint-contracts` when an endpoint changes.
