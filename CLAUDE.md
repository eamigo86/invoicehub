# InvoiceHub

Multi-tenant invoicing SaaS in Django: invoices, PDFs, bank reconciliation with
`remitmatch`, and an AI assistant (OpenRouter) with an MCP server.
Specs: `docs/rfc/0000-overview.md` and `docs/rfc/0002`–`0007`. Read the relevant RFC before
working on an area.

## Stack

Python 3.13, Django 6.1.x, PostgreSQL 17 with Row-Level Security, Celery + Redis,
GraphQL with django-graphex, Django templates + HTMX + Tailwind (standalone CLI, no Node),
Django admin with django-unfold, WeasyPrint, django-storages (GCS), deployed on Cloud Run.

## Apps

`core`, `tenancy`, `accounts`, `billing`, `reconciliation`, `assistant`.
Business logic lives in each app's `services.py`; views, GraphQL resolvers and MCP tools
only call services.

## Commands

```bash
docker compose up -d                       # web, worker, postgres, redis, mailpit
uv run ruff check . && uv run ruff format --check .
uv run mypy .
uv run pytest --cov                        # needs postgres + redis running
uv run pytest -m isolation                 # tenant isolation suite
uv run python manage.py makemigrations --check
```

Run lint, mypy and the full test suite before saying a task is done.

## Rules

- Every business model inherits from `TenantModel`. Every unique constraint includes `tenant_id`.
- RLS is enabled on every tenant table; the app's DB role has no `BYPASSRLS`.
- Any new model, view, GraphQL field or AI tool gets a case in the isolation suite.
- Money is integer cents (`BigIntegerField`) plus a currency code. Never floats.
- Issued invoices are immutable; corrections are credit notes.
- Celery tasks take `tenant_id` explicitly and are idempotent.
- Strict TDD for all code, not only services: write a failing test and watch it fail (RED),
  write the minimum code to make it pass (GREEN), then refactor. No production code without a
  failing test first. Config-only files (Dockerfile, Compose, CI, pre-commit) are verified by
  running them.
- English for code, comments, commits (Conventional Commits) and docs.

## Docstrings

- **Complete Google style on the public API.** Every public module, class, function and method
  has a complete Google-style docstring, with exact, non-empty sections. Public means: an
  importable module; a name without a leading underscore or listed in `__all__`; a class member
  that is not private. `Args:` also documents `*args` and `**kwargs`.
- **Types live in the signature.** Parameters and return values have type hints. `Args:`,
  `Returns:`/`Yields:` and `Raises:` describe behavior and never repeat the types.
- **Plain text.** No docstring contains backticks, including private, nested and dunder
  functions.

## Ask me first

- Before pushing to `main`, changing CI secrets, or deploying.
- Before adding a dependency not listed in the RFCs.
- Never handle my passwords or tokens; tell me what to configure and wait.
