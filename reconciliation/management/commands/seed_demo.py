"""Management command behind make demo: seed the demo and let the worker reconcile it."""

import os
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from reconciliation.models import BankStatementImport, ReconciliationRun
from reconciliation.seed import DEMO_EMAIL, DEMO_PASSWORD, DEMO_SEED, seed_demo
from reconciliation.services import run_reconciliation
from tenancy.context import tenant_context
from tenancy.models import ApiToken, Membership
from tenancy.permissions import Principal
from tenancy.tokens import hash_token, issue_token

POLL_SECONDS = 0.5
"""Pause between two reads of a row the worker is expected to finish."""
RESET_HINT = "Run docker compose down -v to start over."
"""Appended to every failure, since a rerun resumes the same half-finished demo."""
ADMIN_URL = "http://localhost:8010"
"""Admin origin of the Compose default port."""


def _wait_for[T: BankStatementImport | ReconciliationRun](
    model: type[T], tenant_id: UUID, pk: UUID, timeout: float, what: str
) -> T:
    """Poll a row until the worker leaves its pending state.

    Args:
        model: Model of the row, an import or a run.
        tenant_id: Tenant that owns the row.
        pk: Primary key of the row.
        timeout: Seconds to wait before giving up.
        what: Name of the step, for the error messages.

    Returns:
        The finished row.

    Raises:
        CommandError: When the worker does not finish in time or the step failed.
    """
    deadline = time.monotonic() + timeout
    row: T
    while True:
        # Each read is its own transaction, so the commits of the worker become visible.
        with tenant_context(tenant_id):
            row = model.objects.get(pk=pk)
        if row.status != model.Status.PENDING:
            break
        if time.monotonic() >= deadline:
            raise CommandError(
                f"The worker did not finish the {what}; check docker compose logs worker. "
                + RESET_HINT
            )
        time.sleep(POLL_SECONDS)
    if row.status == model.Status.FAILED:
        raise CommandError(f"The {what} failed: {row.error}. {RESET_HINT}")
    return row


class Command(BaseCommand):
    """Seed the demo company, import its statement and reconcile it through the worker."""

    help = "Seed the demo company and wait for the worker to import and reconcile its statement."

    def add_arguments(self, parser: CommandParser) -> None:
        """Declare the command options.

        Args:
            parser: Parser to add the options to.
        """
        parser.add_argument("--seed", type=int, default=DEMO_SEED, help="Seed of the billing data.")
        parser.add_argument(
            "--timeout",
            type=float,
            default=120,
            help="Seconds to wait for the worker at each step.",
        )

    def _login(self) -> None:
        """Print the admin address and the demo login."""
        self.stdout.write(f"  Admin:     {ADMIN_URL}/admin/")
        if settings.GRAPHIQL_TOKEN:
            self.stdout.write(f"  GraphiQL:  {ADMIN_URL}/graphiql/")
        self.stdout.write(f"  Email:     {DEMO_EMAIL}")
        self.stdout.write(f"  Password:  {DEMO_PASSWORD}")

    def _ensure_graphiql_token(self, membership: Membership) -> None:
        """Make sure the demo tenant has the public read-only token behind GraphiQL.

        Does nothing when GRAPHIQL_TOKEN is empty or the demo tenant already has the token.

        Args:
            membership: Membership of the demo user, who owns the demo tenant.

        Raises:
            CommandError: When the token value already belongs to another tenant.
        """
        raw = settings.GRAPHIQL_TOKEN
        if not raw:
            return
        existing = ApiToken.objects.filter(token_hash=hash_token(raw)).first()
        if existing is None:
            issue_token(
                Principal.from_membership(membership),
                "GraphiQL demo",
                ["invoices:read"],
                created_by=membership.user,
                allow_introspection=True,
                plaintext=raw,
            )
        elif existing.tenant_id != membership.tenant_id:
            raise CommandError("GRAPHIQL_TOKEN already belongs to a token of another tenant.")

    def handle(self, *args: Any, **options: Any) -> None:
        """Seed the demo, wait for the import and the run, and print the outcome.

        Args:
            *args: Positional arguments, unused.
            **options: Parsed options.

        Raises:
            CommandError: When DEBUG is off, or the worker fails or does not answer in time.
        """
        if not settings.DEBUG:
            raise CommandError(
                "The demo user has a published password, so seed_demo only runs with DEBUG on."
            )
        timeout = options["timeout"]
        membership = Membership.objects.filter(user__email=DEMO_EMAIL).first()
        if membership is None:
            demo = seed_demo(options["seed"])
            tenant_id, user_id = demo.tenant_id, demo.user_id
            membership = Membership.objects.get(user_id=user_id, tenant_id=tenant_id)
        else:
            tenant_id, user_id = membership.tenant_id, membership.user_id
        self._ensure_graphiql_token(membership)
        with tenant_context(tenant_id):
            statement_id = BankStatementImport.objects.earliest("created_at", "pk").pk
        statement = _wait_for(BankStatementImport, tenant_id, statement_id, timeout, "import")
        path = Path(settings.MEDIA_ROOT) / "demo" / "bank_statement.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with tenant_context(tenant_id), statement.file.open("rb") as file:
            path.write_bytes(file.read())
        with tenant_context(tenant_id):
            existing = ReconciliationRun.objects.order_by("created_at", "pk").first()
            if existing is None:
                principal = Principal.from_membership(
                    Membership.objects.get(user_id=user_id, tenant_id=tenant_id)
                )
                existing = run_reconciliation(principal)
            run_id = existing.pk
        run = _wait_for(ReconciliationRun, tenant_id, run_id, timeout, "run")
        self.stdout.write("Demo ready.")
        self._login()
        relative = os.path.relpath(path, settings.BASE_DIR)
        self.stdout.write(f"  Statement: {relative}, {statement.imported} rows imported")
        self.stdout.write(
            f"  Matches:   {run.applied_count} applied, {run.suggested_count} suggested and "
            f"{run.ambiguous_count} ambiguous to review, "
            f"{run.unmatched_transactions} bank rows unmatched"
        )
        self.stdout.write(f"  Review:    {ADMIN_URL}/admin/reconciliation/matchproposal/")
