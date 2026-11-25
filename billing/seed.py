"""Fictitious US billing data for demos and local development.

The seed writes only through the billing services, acting as an owner-level system principal,
so every rule of the real write path applies to it. Content comes from Faker en_US seeded by the
caller: the same seed gives the same names, amounts and statuses. Dates are relative to today in
the tenant timezone, so a demo always has overdue invoices. Issuing reads the clock, so the
clock is set back while each document is issued, which dates it in the past like real history.
"""

import uuid
import zoneinfo
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.utils import timezone
from faker import Faker

from billing.models import Customer, Invoice, Payment, Product, TaxRate
from billing.services import (
    DraftLine,
    draft_credit_note,
    issue_invoice,
    record_payment,
    save_customer,
    save_draft,
    save_product,
    save_tax_rate,
    update_issuer,
)
from tenancy.context import tenant_context
from tenancy.models import Tenant
from tenancy.permissions import Permission, Principal

__all__ = ["seed_tenant"]

US_TIMEZONES = ["America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"]
CATALOG = [
    "Consulting hour",
    "Website maintenance",
    "Design sprint",
    "Training day",
    "Support plan",
    "Hardware kit",
]
# Each issued invoice is (days since issue, percent of the total paid): None is unpaid.
ISSUED_PLAN: list[tuple[int, int | None]] = [
    (75, None),  # overdue
    (50, None),  # overdue
    (18, None),  # due soon, also gets the partial credit note
    (10, None),
    (3, None),
    (35, 40),  # partially paid
    (20, 60),  # partially paid
    (40, 100),  # paid
    (25, 100),  # paid
]
DRAFTS = 2
VOIDED_AGO, VOIDED_CREDIT_AGO = 55, 50
PARTIAL_CREDIT_AGO = 5


@contextmanager
def _days_ago(today: date, tz: str, days: int) -> Iterator[None]:
    """Make the Django clock read noon of an earlier day, so documents are dated in the past.

    Args:
        today: Today in the tenant timezone.
        tz: Timezone name of the tenant.
        days: How many days before today the clock shows.

    Yields:
        Nothing; the clock is set back while the block runs.
    """
    noon = datetime.combine(today - timedelta(days=days), time(12), zoneinfo.ZoneInfo(tz))
    # ponytail: patches the process-wide clock, so seed only from the command or a dedicated
    # worker, never inside a web process; an explicit clock argument on the services is the
    # upgrade path if backdating is ever needed elsewhere.
    with mock.patch("django.utils.timezone.now", return_value=noon):
        yield


def _lines(fake: Faker, products: list[Product]) -> list[DraftLine]:
    """Draw one to three lines from the catalog.

    Args:
        fake: Seeded generator.
        products: Catalog to draw from.

    Returns:
        The draft lines, each with its product and default taxes.
    """
    return [
        DraftLine(
            product.name,
            Decimal(fake.random_int(1, 10)),
            product.unit_price,
            discount_bp=fake.random_element([0, 0, 500, 1000]),
            tax_rates=tuple(product.default_taxes.order_by("name")),
            product=product,
        )
        for product in fake.random_elements(products, length=fake.random_int(1, 3), unique=True)
    ]


def _fill_catalog(
    fake: Faker, principal: Principal
) -> tuple[list[TaxRate], list[Customer], list[Product]]:
    """Create the tax rates, customers and products.

    Args:
        fake: Seeded generator.
        principal: Acting principal.

    Returns:
        The tax rates, customers and products created.
    """
    state, county, city = fake.state_abbr(), fake.city(), fake.city()
    rates = [
        save_tax_rate(principal, TaxRate(name=name, rate_bp=fake.random_int(low, high) * 5))
        for name, low, high in [
            (f"{state} state sales tax", 80, 140),
            (f"{county} county tax", 10, 40),
            (f"{city} city tax", 5, 20),
        ]
    ]
    customers = [
        save_customer(
            principal,
            Customer(
                name=fake.unique.company(),
                tax_id=f"{fake.random_int(10, 99)}-{fake.random_number(7, fix_len=True)}",
                emails=[fake.company_email()],
                address_line1=fake.street_address(),
                city=fake.city(),
                state=fake.state_abbr(),
                postal_code=fake.postcode(),
            ),
        )
        for _ in range(8)
    ]
    products = [
        save_product(
            principal,
            Product(name=name, unit_price=fake.random_int(20, 400) * 500),
            default_taxes=rates[: fake.random_int(0, 3)],
        )
        for name in CATALOG
    ]
    return rates, customers, products


def seed_tenant(tenant_id: uuid.UUID, seed: int) -> dict[str, int]:
    """Fill a tenant with fictitious US billing data.

    The tenant gets complete issuer details, tax rates, customers, products and a dozen invoices:
    drafts, unpaid, overdue, partially paid and paid ones, one voided by a full credit note, and
    a partial credit note on another.

    Args:
        tenant_id: Tenant to fill, which should be empty.
        seed: Seed of the generator; the same seed gives the same content.

    Returns:
        How many rows of each kind the tenant holds afterwards.
    """
    fake = Faker("en_US")
    fake.seed_instance(seed)
    principal = Principal(tenant_id, frozenset(Permission))
    with tenant_context(tenant_id):
        tenant = Tenant.objects.get(pk=tenant_id)
        tenant.legal_name = fake.company()
        tenant.ein = f"{fake.random_int(10, 99)}-{fake.random_number(7, fix_len=True)}"
        tenant.address_line1, tenant.city = fake.street_address(), fake.city()
        tenant.state, tenant.postal_code = fake.state_abbr(), fake.postcode()
        tenant.default_currency, tenant.timezone = "USD", fake.random_element(US_TIMEZONES)
        update_issuer(principal, tenant)
        today = timezone.now().astimezone(zoneinfo.ZoneInfo(tenant.timezone)).date()
        _, customers, products = _fill_catalog(fake, principal)

        for _ in range(DRAFTS):
            save_draft(
                principal, Invoice(customer=fake.random_element(customers)), _lines(fake, products)
            )
        history: list[tuple[Invoice, list[DraftLine], int, int | None]] = []
        # Oldest first, because the number series only moves forward in time.
        for ago, paid in sorted([*ISSUED_PLAN, (VOIDED_AGO, None)], reverse=True):
            lines = _lines(fake, products)
            draft = save_draft(principal, Invoice(customer=fake.random_element(customers)), lines)
            with _days_ago(today, tenant.timezone, ago):
                history.append((issue_invoice(principal, draft), lines, ago, paid))
        for invoice, _, ago, paid in history:
            if paid:
                cents = invoice.total * paid // 100
                payment = Payment(
                    amount=cents,
                    currency=invoice.currency,
                    value_date=today - timedelta(days=ago - 5),
                    reference=f"ACH {fake.bothify('??#####').upper()}",
                )
                record_payment(principal, payment, [(invoice, cents)])
        voided = next(invoice for invoice, _, ago, _ in history if ago == VOIDED_AGO)
        partial, partial_lines, _, _ = next(item for item in history if item[2] == 18)
        credits = [
            (draft_credit_note(principal, voided), VOIDED_CREDIT_AGO),
            # One unit of the first line, which never exceeds what the invoice charged.
            (
                draft_credit_note(
                    principal, partial, [replace(partial_lines[0], quantity=Decimal(1))]
                ),
                PARTIAL_CREDIT_AGO,
            ),
        ]
        for note, ago in credits:
            with _days_ago(today, tenant.timezone, ago):
                issue_invoice(principal, note)
        return {
            "tax rates": TaxRate.objects.count(),
            "customers": Customer.objects.count(),
            "products": Product.objects.count(),
            "invoices": Invoice.objects.filter(kind=Invoice.Kind.INVOICE).count(),
            "credit notes": Invoice.objects.filter(kind=Invoice.Kind.CREDIT_NOTE).count(),
            "payments": Payment.objects.count(),
        }
