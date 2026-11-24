# InvoiceHub

InvoiceHub is a multi-tenant invoicing SaaS for SMBs and freelancers. It issues invoices and PDFs,
reconciles bank payments against invoices with [remitmatch](docs/rfc/0001-remitmatch.md), and
includes an AI assistant exposed through an MCP server. Tenant isolation is enforced twice: by the
application and by PostgreSQL Row-Level Security.

## Architecture

A Django monolith with three entry points (HTMX UI, GraphQL API, MCP server) that share one
services layer, plus a Celery worker for everything slow. The web app and the worker use
PostgreSQL (with Row-Level Security) and Redis as the task queue.

```mermaid
flowchart TB
    subgraph clients[Clients]
        B["Browser (HTMX)<br/>Tenant user"]
        G["GraphQL client<br/>Integrator with token"]
        A["Agent via MCP<br/>Claude or another client"]
    end
    subgraph web["Web · Django on Cloud Run"]
        V[HTMX views]
        Q["GraphQL API (graphex)"]
        M["MCP server + assistant"]
        S["Domain services<br/>permissions · audit · business logic"]
    end
    subgraph worker["Worker · Celery on Cloud Run"]
        P[PDFs with WeasyPrint]
        R["Import and reconciliation (remitmatch)"]
        E[Email delivery]
    end
    subgraph shared[Shared by web and worker]
        PG[(PostgreSQL · RLS)]
        RD[(Redis · task queue)]
        GCS[(GCS · PDFs and statements)]
        OR[OpenRouter · external LLM]
    end
    B --> V
    G --> Q
    A --> M
    V --> S
    Q --> S
    M --> S
    S -- "reads, writes and enqueues" --> shared
    RD --> R
```

Design documents live in [`docs/rfc/`](docs/rfc/). They are drafts and may change.

## Run locally

Requirements: Docker, [uv](https://docs.astral.sh/uv/) and Python 3.13.

```bash
docker compose up -d --build     # web, worker, postgres, redis, mailpit
docker compose exec web python manage.py createsuperuser
```

Default host ports (override with environment variables or a `.env` file):

| Service | URL / port | Variable |
| --- | --- | --- |
| Web (admin at `/admin/`) | http://localhost:8010 | `WEB_PORT` |
| PostgreSQL | localhost:5442 | `POSTGRES_PORT` |
| Redis | localhost:6390 | `REDIS_PORT` |
| Mailpit UI | http://localhost:8025 | `MAILPIT_UI_PORT` |
| Mailpit SMTP | localhost:1025 | `MAILPIT_SMTP_PORT` |

Containers connect to PostgreSQL as the unprivileged `invoicehub_app` role; only the migrate step
uses `invoicehub_migrator`.

### Choosing a tenant in the admin

Tenant models are hidden in the admin until a tenant is active. Superusers pick one with the
**Activate** row action on the Tenant admin, which stores it in the session. This is provisional:
real tenant selection with membership checks arrives with authentication (RFC 0002).

### Checks and tests on the host

```bash
cp .env.example .env             # connects as invoicehub_migrator on localhost:5442
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy .
uv run python manage.py makemigrations --check
uv run pytest --cov              # needs the Compose postgres and redis running
uv run pytest -m isolation       # tenant isolation suite only (also part of the full run)
uv run pre-commit install        # optional: run the checks on every commit
```

Host-side commands (pytest, migrate, runserver) connect as the migrator, which owns the tables;
`FORCE ROW LEVEL SECURITY` keeps the tenant policies applying to it as well.

## Status

Early development. The project scaffold, Docker stack, database roles, tooling and CI are in
place; the tenancy foundation is in progress: the tenant registry, request-scoped tenant context,
tenant-scoped base model and Row-Level Security are done, with `Customer` as the first protected
model; the parametrized isolation suite and admin tenant selection are in place.

## License

[MIT](LICENSE)
