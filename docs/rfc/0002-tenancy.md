# RFC 0002 — Multi-tenancy and authentication

**Status:** draft

All tenants share tables with a `tenant_id` column, and Postgres enforces isolation with Row-Level Security; the application also filters by tenant as a second barrier.

## Context

An isolation bug (a customer seeing another's invoices) is the worst possible failure in an invoicing SaaS. The design must ensure that forgetting a filter is not enough to leak data.

## Design

1. **Base model.** Every business model inherits from `TenantModel` (FK `tenant`, composite index `(tenant_id, ...)` and a manager that filters by the current tenant).
2. **Current tenant.** A middleware resolves the tenant from the session (UI) or the token (API, MCP) and stores it in a `contextvars.ContextVar`. Celery tasks receive the `tenant_id` as an argument and set it at startup.
3. **Row-Level Security.** When the transaction is opened, `SET LOCAL app.tenant_id = '<id>'` is executed. Each table has the policy `USING (tenant_id = current_setting('app.tenant_id')::uuid)`. The app connects with a role without `BYPASSRLS`; migrations use a different role.
4. **Uniqueness.** Unique constraints always include `tenant_id` (e.g. `(tenant_id, series_id, number)`).
5. **Isolation tests.** A parameterized suite creates two tenants and verifies, for every model, GraphQL query, view and AI tool, that tenant A can neither read nor modify anything of B. It is mandatory in CI.

## Authentication and roles

- **UI:** Django sessions with django-allauth (email + password, email verification); a user can belong to several tenants and chooses an active one.
- **API and MCP:** per-tenant API tokens, with scopes (`invoices:read`, `invoices:write`, `reconciliation:write`), stored hashed and revocable.
- **Roles:** `owner`, `admin`, `accountant`, `viewer`, with permissions checked at a single point (permissions service) used by views, resolvers and AI tools.

## Alternatives considered

| Alternative | Why it was rejected |
| --- | --- |
| Schema per tenant (django-tenants) | Migrations grow linearly with the number of tenants, connections and cache per schema; more isolation than an SMB needs |
| Database per tenant | Cost and operations disproportionate for the project |
| Application-level filter only | A forgotten `.objects.all()` is enough for a leak |

## Risks

- **Connections reused with a previous tenant:** `SET LOCAL` scopes the value to the transaction and `ATOMIC_REQUESTS` guarantees there is always one.
- **RLS performance:** indexes starting with `tenant_id` and measurement with `EXPLAIN ANALYZE` in the performance suite.
