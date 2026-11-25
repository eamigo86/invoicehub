"""The demo company for make demo: billing data plus a bank statement that exercises matching.

Everything is written through services. The statement is built from the open invoices of the
billing seed, so once imported and reconciled it yields applied auto matches, one suggested
match, one ambiguous match and unmatched bank rows.
"""

import csv
import io
import uuid
import zoneinfo
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from allauth.account.models import EmailAddress
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from billing.models import Customer, Invoice
from billing.seed import seed_tenant
from billing.services import DraftLine, issue_invoice, save_draft
from reconciliation.models import BankAccount
from reconciliation.services import create_import, save_bank_account
from tenancy.context import tenant_context
from tenancy.models import Membership
from tenancy.permissions import Principal
from tenancy.services import create_tenant

__all__ = [
    "DEMO_COMPANY",
    "DEMO_EMAIL",
    "DEMO_PASSWORD",
    "DEMO_SEED",
    "RETAINER_CENTS",
    "Demo",
    "seed_demo",
]

DEMO_EMAIL = "demo@example.com"
"""Login email of the demo user."""
DEMO_PASSWORD = "invoicehub-demo"  # noqa: S105 - fictitious, the demo command needs DEBUG
"""Password of the demo user; fictitious, because the demo command refuses to run without DEBUG."""
DEMO_SEED = 2026
"""Default seed of the fictitious billing data."""
DEMO_COMPANY = "Demo Company"
"""Name of the demo tenant."""
RETAINER_CENTS = 150_000
"""Total in cents of each of the two same-customer retainer invoices."""


@dataclass(frozen=True)
class Demo:
    """What the demo seed created.

    Attributes:
        tenant_id: Tenant of the demo company.
        user_id: Primary key of the demo user.
        statement: The bank statement CSV, as UTF-8 bytes.
        statement_import_id: Import created from the statement, still to be processed.
    """

    tenant_id: uuid.UUID
    user_id: int
    statement: bytes
    statement_import_id: uuid.UUID


def _money(cents: int) -> str:
    """Format integer cents as a plain decimal amount, without floats.

    Args:
        cents: Signed amount in cents.

    Returns:
        The amount with thousands separators and two decimals, for example 1,500.00.
    """
    sign = "-" if cents < 0 else ""
    whole, fraction = divmod(abs(cents), 100)
    return f"{sign}{whole:,}.{fraction:02d}"


def _statement(open_invoices: list[Invoice], pair: list[Invoice], today: date) -> bytes:
    """Build the statement CSV for the open invoices and the retainer pair.

    The two oldest open invoices get no row, the newest gets a mistyped reference and the rest
    are paid in full with their exact reference.

    Args:
        open_invoices: Open invoices, oldest first, read before the pair was created.
        pair: The two same-customer, same-total invoices.
        today: Date of every row.

    Returns:
        The CSV as UTF-8 bytes.
    """
    rows: list[tuple[str, int, str]] = []
    for invoice in open_invoices[2:]:
        reference = invoice.reference
        if invoice is open_invoices[-1]:
            prefix, number = reference.rsplit("-", 1)
            reference = f"{prefix}-{number[1:]}"
        name = (invoice.customer_details or {})["name"]
        rows.append((f"ACH PAYMENT {reference}", invoice.balance_due(), name))
    first, second = pair
    rows.append(
        (
            f"ACH PAYMENT {first.reference} {second.reference}",
            RETAINER_CENTS,
            (first.customer_details or {})["name"],
        )
    )
    rows += [
        ("STRIPE TRANSFER", 231_840, "Stripe"),
        ("INTEREST PAYMENT", 87, ""),
        ("MONTHLY SERVICE FEE", -2_500, ""),
    ]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["Date", "Description", "Amount", "Payer"])
    for description, cents, payer in rows:
        writer.writerow([today.strftime("%m/%d/%Y"), description, _money(cents), payer])
    return out.getvalue().encode()


def seed_demo(seed: int = DEMO_SEED) -> Demo:
    """Create the demo user and company, fill it and queue its bank statement import.

    All of it happens in one transaction, so a failure leaves nothing behind.

    Args:
        seed: Seed of the fictitious billing data.

    Returns:
        The ids of what was created and the statement that was uploaded.
    """
    with transaction.atomic():
        user = User.objects.create_superuser("demo", DEMO_EMAIL, DEMO_PASSWORD)
        EmailAddress.objects.create(user=user, email=DEMO_EMAIL, primary=True, verified=True)
        tenant = create_tenant(user, DEMO_COMPANY)
        seed_tenant(tenant.pk, seed)
        with tenant_context(tenant.pk):
            principal = Principal.from_membership(Membership.objects.get(user=user, tenant=tenant))
            today = timezone.now().astimezone(zoneinfo.ZoneInfo(tenant.timezone)).date()
            open_invoices = sorted(
                (
                    invoice
                    for invoice in Invoice.objects.filter(
                        kind=Invoice.Kind.INVOICE, status__in=Invoice.PAYABLE_STATUSES
                    )
                    if invoice.balance_due() > 0
                ),
                key=lambda invoice: (invoice.issue_date, invoice.reference),
            )
            customer = Customer.objects.order_by("name").first()
            pair = [
                issue_invoice(
                    principal,
                    save_draft(
                        principal,
                        Invoice(customer=customer),
                        [DraftLine("Monthly retainer", Decimal(1), RETAINER_CENTS)],
                    ),
                )
                for _ in range(2)
            ]
            account = save_bank_account(
                principal,
                BankAccount(
                    name="Operating account",
                    account_identifier="4321",
                    date_column="Date",
                    amount_column="Amount",
                    description_column="Description",
                    payer_column="Payer",
                ),
            )
            content = _statement(open_invoices, pair, today)
            statement_import = create_import(principal, account, content)
        return Demo(tenant.pk, user.pk, content, statement_import.pk)
