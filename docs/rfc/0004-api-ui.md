# RFC 0004 — GraphQL API and UI

**Status:** draft

The public API is GraphQL with django-graphex and the UI is server-rendered with HTMX; both call the same services layer, so business logic and permissions live in a single place.

## Layers

1. **Domain services** (`billing/services.py`, etc.): validate, apply permissions and write audit records. The only point with business logic.
2. **GraphQL** (django-graphex): types derived from the models, mutations that delegate to the services.
3. **UI** (Django views + HTMX): forms and fragments that delegate to the same services.
4. **MCP server** (RFC 0006): tools that delegate to the same services.

## GraphQL API

- **Endpoint** `/graphql/` authenticated with a per-tenant API token (RFC 0002); the tenant comes from the token, never from an argument.
- **Queries:** invoices, customers, products, payments and reconciliation sessions, with nested filters and django-graphex cursor pagination.
- **Mutations:** create/edit draft, issue, send, issue a credit note, accept or reject a reconciliation match. Validation with Pydantic.
- **Protection:** query depth and cost limits, per-token rate limiting, introspection disabled in production except for development tokens.
- **Subscriptions** (optional): import and reconciliation progress via Django Channels.
- **Errors:** typed domain errors in the mutation payload, not generic exceptions.

Using your own library in a real project is part of the value: any gap that appears becomes an improvement to django-graphex with its issue and its PR.

## UI

- Django templates + HTMX for partial interactions (invoice lines, reconciliation queue, assistant chat).
- Tailwind with the standalone CLI, no Node; the compiled CSS is generated in the Docker build.
- Streaming of assistant responses with SSE (HTMX `sse` extension).
- Keyboard accessible and usable on mobile; no SPA.

## Admin

- Django admin themed with django-unfold, for internal operation and support. Tenant users work in the HTMX UI, not in the admin.
- It goes through the same tenant middleware and Row-Level Security as any other view, so it only shows the active tenant's rows. There is no cross-tenant admin: the app's database role has no `BYPASSRLS`.
- Writes on business models go through the services (`save_model` and `delete_model` delegate to them), so the admin cannot bypass rules such as invoice immutability.
- Like any other view, every admin registered for a tenant model gets a case in the isolation suite.

## Alternatives considered

| Alternative | Why it was rejected |
| --- | --- |
| REST with django-ninja | More common in job postings, but it does not showcase your library; it can be added later on top of the same services |
| Strawberry or graphene-django | Standard, but the goal is to use and improve django-graphex |
| Datastar instead of HTMX | Less well known; HTMX is what is expected in the Django ecosystem |
| SPA with React | Outside the backend profile and adds Node to the build |
