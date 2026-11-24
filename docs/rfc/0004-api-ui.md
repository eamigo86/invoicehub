# RFC 0004 — GraphQL API and UI

**Status:** draft

The public API is GraphQL with django-graphex and the UI is server-rendered with HTMX; both call the same services layer, so business logic and permissions live in a single place.

## Layers

1. **Domain services** (`billing/services.py`, etc.): validate, apply permissions and write audit records. The only point with business logic.
2. **GraphQL** (django-graphex): types derived from the models, mutations that delegate to the services.
3. **UI** (Django views + HTMX): forms and fragments that delegate to the same services.
4. **MCP server** (RFC 0006): tools that delegate to the same services.

## Why GraphQL if the UI is HTMX

The UI does not use GraphQL: the HTMX views call the domain services directly and return HTML. GraphQL is the **public API for integrators**, the usual case in invoicing: online stores, ERPs or CRMs that create invoices or query payments from their own system.

GraphQL fits that case because each integrator asks for exactly what it needs in a single query (for example, overdue invoices with their lines, their customer and their payments), without custom endpoints for every combination.

InvoiceHub is also the first real user of django-graphex: the gaps that show up when using it in a project of this size are fixed in the library, with their issue and PR linked from here.

## GraphQL API

- **Endpoint** `/graphql/` authenticated with a per-tenant API token (RFC 0002); the tenant comes from the token, never from an argument.
- **Queries:** invoices, customers, products, payments and reconciliation sessions, with nested filters and django-graphex cursor pagination.
- **Mutations:** create/edit draft, issue, send, issue a credit note, accept or reject a reconciliation match. Validation with Pydantic.
- **Protection:** query depth and cost limits, per-token rate limiting, introspection disabled in production except for development tokens.
- **Subscriptions** (optional): import and reconciliation progress via Django Channels.
- **Errors:** typed domain errors in the mutation payload, not generic exceptions.

## API consumers

An API without consumers proves nothing, so v1 includes three:

1. **Example integration** (`examples/shop_integration/`): a script that simulates an online store and, when an order is closed, creates the customer if it does not exist, creates the draft invoice and issues it via GraphQL. It uses an API token with the `invoices:write` scope and runs in CI against the test environment.
2. **Explorer in the demo:** GraphiQL available in the public demo with a read-only token (`invoices:read`) on the demo tenant, so anyone can try queries without signing up.
3. **Contract tests:** the schema is exported to `schema.graphql` and versioned in the repo. CI compares the generated schema with the versioned one and fails on breaking changes (removed fields, changed types) that do not come with a deprecation note.

### Example integrator query

```graphql
query OverdueInvoices {
  invoices(filter: { status: { in: [ISSUED, PARTIALLY_PAID] }, dueDate: { lt: "2026-11-01" } }, first: 50) {
    edges {
      node {
        number
        totalMinor
        currency
        customer { name taxId }
        payments { amountMinor valueDate }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
```

The exact filter and pagination syntax is whatever django-graphex generates; this example is updated once the schema is settled.

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
| REST with django-ninja | More common in the industry and simpler for basic integrations. Rejected for v1 because the goal is to use and improve django-graphex; since the logic lives in the services, a REST API can be added later without touching the domain |
| Strawberry or graphene-django | Standard, but the goal is to use and improve django-graphex |
| Datastar instead of HTMX | Less well known; HTMX is what is expected in the Django ecosystem |
| SPA with React | Outside the backend profile and adds Node to the build |
