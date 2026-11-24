# RFC 0007 — Infrastructure and observability

**Status:** draft

A single Docker image is deployed to Cloud Run as both the web service and the Celery worker, with managed Cloud SQL and Redis; every change goes through CI and reaches production with a merge to `main`.

## Environments

| Environment | Where | Data |
| --- | --- | --- |
| Local | Docker Compose: web, worker, Postgres, Redis, Mailpit | Seed with 2 fictitious US tenants generated with Faker |
| CI | GitHub Actions with Postgres and Redis services | Fixtures + isolation suite |
| Production (demo) | GCP: Cloud Run, Cloud SQL, managed Redis, GCS | Demo tenant with Faker data (USD, sales tax, EIN) that resets every night |

## Deployment on GCP

- **Web:** Cloud Run with Gunicorn + Uvicorn workers; scales to zero outside business hours.
- **Celery worker:** Cloud Run with 1 minimum instance and CPU always allocated (workers do not receive HTTP requests).
- **Periodic tasks:** Cloud Scheduler calls an authenticated internal endpoint that enqueues the task (demo reset, overdue reminders), instead of a beat process.
- **Database:** Cloud SQL for PostgreSQL, connection via the Cloud SQL connector; migrations as a Cloud Run Job before each deployment.
- **Redis:** Memorystore, or an external managed Redis if the monthly cost is not worth it for a demo.
- **Secrets:** Secret Manager mounted as environment variables.
- **Infrastructure as code (optional):** Minimal Terraform for the resources above; if time runs short, the `gcloud` commands are documented.

## CI/CD

1. On every PR: ruff, mypy, pytest (including isolation and AI evals with a cheap model), image build and Tailwind build.
2. On merge to `main`: image to Artifact Registry, migrations, deployment of web and worker, smoke test against the public URL.
3. GitHub authentication with GCP via Workload Identity Federation, without service keys.

## Observability

- **Traces:** OpenTelemetry in Django, Celery, psycopg and LLM calls; exported to Cloud Trace. A trace follows an import from upload to applied matches.
- **Errors:** Sentry with `tenant_id` and the release as tags.
- **Logs:** Structured JSON with `request_id`, `tenant_id` and `trace_id`.
- **Business metrics:** invoices issued, % of automatic reconciliation, LLM cost per tenant.
- **Health checks:** `/healthz` (process alive) and `/readyz` (database and Redis reachable).

## Alternatives considered

| Alternative | Why it was rejected |
| --- | --- |
| GKE Autopilot | More powerful but more operations than a demo needs |
| A VM with Docker Compose | Cheap, but it does not demonstrate managed deployment or scaling |
| Celery beat as a process | One more always-on process; Cloud Scheduler replaces it at no extra cost |
