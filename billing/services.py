"""Billing services: the write path for customers and issuer details.

Views, the admin and later the API call these functions, so audit logging and business rules
have a single place to live.

Cross-tenant references are refused by validation: full_clean checks a foreign key through the
related model's base manager, which Row-Level Security limits to the current tenant, so a row of
another tenant raises ValidationError.
"""
# ponytail: writes that bypass the services are not checked; composite (tenant_id, id) foreign
# keys would enforce it in the database.

import uuid
import zoneinfo
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from billing.calculations import LineInput, TaxRateInput, invoice_totals
from billing.models import (
    AuditEvent,
    Customer,
    Invoice,
    InvoiceEmail,
    InvoiceLine,
    InvoiceLineTax,
    InvoiceSeries,
    Payment,
    PaymentAllocation,
    Product,
    ProductTaxRate,
    TaxRate,
)
from tenancy.context import require_current_tenant_id
from tenancy.models import Tenant, TenantModel
from tenancy.permissions import Permission, Principal, require_permission

ISSUER_FIELDS = [
    "legal_name",
    "ein",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postal_code",
    "country",
    "default_currency",
    "timezone",
]
"""Tenant columns that update_issuer may write."""


def list_invoices(principal: Principal | None) -> QuerySet[Invoice]:
    """Return the invoices the principal may read in the current tenant.

    Row-Level Security and the tenant manager scope the rows; this adds the permission check.

    Args:
        principal: Acting principal, who needs invoices:read or PermissionDenied is raised.

    Returns:
        A queryset of the invoices of the current tenant.
    """
    require_permission(principal, Permission.INVOICES_READ)
    return Invoice.objects.all()


def list_customers(principal: Principal | None) -> QuerySet[Customer]:
    """Return the customers the principal may read in the current tenant.

    Row-Level Security and the tenant manager scope the rows; this adds the permission check.

    Args:
        principal: Acting principal, who needs invoices:read or PermissionDenied is raised.

    Returns:
        A queryset of the customers of the current tenant.
    """
    require_permission(principal, Permission.INVOICES_READ)
    return Customer.objects.all()


def list_products(principal: Principal | None) -> QuerySet[Product]:
    """Return the products the principal may read in the current tenant.

    Row-Level Security and the tenant manager scope the rows; this adds the permission check.

    Args:
        principal: Acting principal, who needs invoices:read or PermissionDenied is raised.

    Returns:
        A queryset of the products of the current tenant.
    """
    require_permission(principal, Permission.INVOICES_READ)
    return Product.objects.all()


def list_payments(principal: Principal | None) -> QuerySet[Payment]:
    """Return the payments the principal may read in the current tenant.

    Row-Level Security and the tenant manager scope the rows; this adds the permission check.

    Args:
        principal: Acting principal, who needs invoices:read or PermissionDenied is raised.

    Returns:
        A queryset of the payments of the current tenant.
    """
    require_permission(principal, Permission.INVOICES_READ)
    return Payment.objects.all()


def list_tax_rates(principal: Principal | None) -> QuerySet[TaxRate]:
    """Return the tax rates the principal may read in the current tenant.

    Row-Level Security and the tenant manager scope the rows; this adds the permission check.

    Args:
        principal: Acting principal, who needs invoices:read or PermissionDenied is raised.

    Returns:
        A queryset of the tax rates of the current tenant.
    """
    require_permission(principal, Permission.INVOICES_READ)
    return TaxRate.objects.all()


def update_issuer(principal: Principal | None, tenant: Tenant) -> Tenant:
    """Validate and save the issuer details of the current tenant.

    Only the issuer fields are written, so no other tenant column can change through here.

    Args:
        principal: Acting principal, who needs settings:manage or PermissionDenied is raised.
        tenant: Tenant carrying the new issuer values, which must be the current tenant.

    Returns:
        The saved tenant.

    Raises:
        PermissionDenied: When the tenant is not the current tenant.
    """
    require_permission(principal, Permission.SETTINGS_MANAGE)
    if tenant.pk != require_current_tenant_id():
        raise PermissionDenied("tenant is not the current tenant")
    tenant.full_clean(exclude=["name"])
    tenant.save(update_fields=ISSUER_FIELDS)
    return tenant


def save_customer(principal: Principal | None, customer: Customer) -> Customer:
    """Create or update a customer for the current tenant.

    The customer is validated first, so ValidationError is raised for invalid data.

    Args:
        principal: Acting principal, who needs invoices:write or PermissionDenied is raised.
        customer: Customer to persist.

    Returns:
        The saved customer.
    """
    require_permission(principal, Permission.INVOICES_WRITE)
    customer.full_clean(exclude=["tenant"])
    customer.save()
    return customer


def delete_customer(principal: Principal | None, customer: Customer) -> None:
    """Delete a customer.

    Args:
        principal: Acting principal, who needs invoices:write or PermissionDenied is raised.
        customer: Customer to remove.
    """
    require_permission(principal, Permission.INVOICES_WRITE)
    customer.delete()


def _clean(row: TenantModel) -> None:
    """Validate a row of the current tenant, uniqueness within the tenant included.

    Args:
        row: Row to validate, which receives the current tenant when it has none.
    """
    if row.tenant_id is None:
        row.tenant_id = require_current_tenant_id()
    row.full_clean()


def save_tax_rate(principal: Principal | None, tax_rate: TaxRate) -> TaxRate:
    """Create or update a tax rate of the current tenant.

    The tax rate is validated first, so ValidationError is raised for invalid data.

    Args:
        principal: Acting principal, who needs settings:manage or PermissionDenied is raised.
        tax_rate: Tax rate to persist.

    Returns:
        The saved tax rate.
    """
    require_permission(principal, Permission.SETTINGS_MANAGE)
    _clean(tax_rate)
    tax_rate.save()
    return tax_rate


def delete_tax_rate(principal: Principal | None, tax_rate: TaxRate) -> None:
    """Delete a tax rate.

    A rate used on an invoice line tax cannot be deleted, and Django raises ProtectedError.

    Args:
        principal: Acting principal, who needs settings:manage or PermissionDenied is raised.
        tax_rate: Tax rate to remove.
    """
    require_permission(principal, Permission.SETTINGS_MANAGE)
    tax_rate.delete()


@transaction.atomic
def save_product(
    principal: Principal | None,
    product: Product,
    default_taxes: Iterable[TaxRate] | None = None,
) -> Product:
    """Create or update a product, optionally replacing its default taxes.

    Everything is validated, so ValidationError is raised for invalid data, including a tax
    rate of another tenant, and then nothing is stored.

    Args:
        principal: Acting principal, who needs invoices:write or PermissionDenied is raised.
        product: Product to persist.
        default_taxes: Tax rates the product should link to, replacing the current links, or
            None to leave them alone.

    Returns:
        The saved product.
    """
    require_permission(principal, Permission.INVOICES_WRITE)
    _clean(product)
    product.save()
    if default_taxes is not None:
        ProductTaxRate.objects.filter(product=product).delete()
        for rate in dict.fromkeys(default_taxes):
            link = ProductTaxRate(product=product, tax_rate=rate)
            _clean(link)
            link.save()
    return product


def delete_product(principal: Principal | None, product: Product) -> None:
    """Delete a product together with its default tax links.

    A product used on an invoice line cannot be deleted, and Django raises ProtectedError.

    Args:
        principal: Acting principal, who needs invoices:write or PermissionDenied is raised.
        product: Product to remove.
    """
    require_permission(principal, Permission.INVOICES_WRITE)
    product.delete()


def record_event(
    principal: Principal | None,
    action: str,
    entity: str,
    entity_id: uuid.UUID,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append an event to the audit log of the current tenant.

    Services call this inside their own transaction, so the event commits or rolls back with
    the change it describes. The log has no update or delete service. The actor is a token when
    the principal carries a token id, a user when it carries a user id, and the system when
    there is no principal.

    Args:
        principal: Acting principal, or None for work done by the system.
        action: What happened, for example invoice.issued.
        entity: Kind of the entity acted on, for example invoice.
        entity_id: Primary key of that entity.
        before: Relevant state before the action, if any.
        after: Relevant state after the action, if any.

    Returns:
        The saved event.

    Raises:
        PermissionDenied: When the principal belongs to a tenant other than the current one.
    """
    user_id = token_id = None
    if principal is not None:
        if principal.tenant_id != require_current_tenant_id():
            raise PermissionDenied("principal belongs to another tenant")
        user_id, token_id = principal.user_id, principal.token_id
    if token_id is not None:
        kind = AuditEvent.ActorKind.TOKEN
    elif user_id is not None:
        kind = AuditEvent.ActorKind.USER
    else:
        kind = AuditEvent.ActorKind.SYSTEM
    event = AuditEvent(
        actor_kind=kind,
        actor_user_id=user_id,
        actor_token_id=token_id,
        action=action,
        entity=entity,
        entity_id=entity_id,
        before=before,
        after=after,
    )
    event.save()
    return event


class InvalidTransition(Exception):
    """Raised when an invoice is not in a state that allows the requested change."""


@dataclass(frozen=True)
class DraftLine:
    """The input for one line of a draft invoice.

    Attributes:
        description: What is being charged.
        quantity: Amount of units, with up to three decimals.
        unit_price: Price of one unit in cents.
        discount_bp: Line discount in basis points.
        tax_rates: Configured tax rates applied to the discounted net.
        product: Catalog entry the line was prefilled from, if any.
    """

    description: str
    quantity: Decimal
    unit_price: int
    discount_bp: int = 0
    tax_rates: tuple[TaxRate, ...] = ()
    product: Product | None = None


def _snapshot(invoice: Invoice) -> dict[str, Any]:
    """Describe an invoice and its lines as compact JSON-ready data for the audit log.

    Args:
        invoice: Saved invoice to describe.

    Returns:
        The invoice fields and, per line, its amounts and taxes.
    """
    lines = [
        {
            "position": line.position,
            "description": line.description,
            "quantity": str(line.quantity),
            "unit_price": line.unit_price,
            "discount_bp": line.discount_bp,
            "product_id": line.product_id,
            "net": line.net,
            "total": line.total,
            "taxes": [
                {"name": tax.name, "rate_bp": tax.rate_bp, "amount": tax.amount}
                for tax in line.taxes.order_by("id")
            ],
        }
        for line in invoice.lines.all()
    ]
    return {
        "kind": invoice.kind,
        "status": invoice.status,
        "customer_id": str(invoice.customer_id),
        "currency": invoice.currency,
        "notes": invoice.notes,
        "subtotal": invoice.subtotal,
        "tax_total": invoice.tax_total,
        "total": invoice.total,
        "lines": lines,
    }


def _stored_draft(invoice: Invoice) -> Invoice | None:
    """Look up and lock the stored copy of an invoice, requiring it to be a draft.

    The row lock is held until the surrounding transaction ends, so editing and issuing the same
    invoice are serialized.

    Args:
        invoice: Invoice whose stored copy is wanted.

    Returns:
        The stored invoice, or None when it is not stored for the current tenant.

    Raises:
        InvalidTransition: When the stored invoice is not a draft.
    """
    stored = Invoice.objects.select_for_update().filter(pk=invoice.pk).first()
    if stored is not None and stored.status != Invoice.Status.DRAFT:
        raise InvalidTransition(f"invoice is {stored.status}, only a draft can change")
    return stored


@transaction.atomic
def save_draft(principal: Principal | None, invoice: Invoice, lines: list[DraftLine]) -> Invoice:
    """Create or update a draft invoice, replacing its lines and storing the calculated totals.

    The invoice, every line and every line tax are validated, so ValidationError is raised for
    invalid data, including a customer, product or tax rate of another tenant, and then nothing
    is stored. Each tax keeps the name and rate of its tax rate at this moment. The change is
    recorded in the audit log as invoice.created or invoice.updated. InvalidTransition is raised
    when the stored invoice is not a draft.

    Args:
        principal: Acting principal, who needs invoices:write or PermissionDenied is raised.
        invoice: Invoice to persist, which becomes a draft. A new invoice without a currency
            gets the default currency of the tenant.
        lines: Lines of the invoice in order, replacing the stored ones.

    Returns:
        The saved invoice with its totals.

    Raises:
        ValidationError: When a line is out of range.
    """
    require_permission(principal, Permission.INVOICES_WRITE)
    stored = _stored_draft(invoice)
    before = _snapshot(stored) if stored else None
    try:
        totals = invoice_totals(
            [
                LineInput(
                    line.quantity,
                    line.unit_price,
                    line.discount_bp,
                    tuple(TaxRateInput(rate.name, rate.rate_bp) for rate in line.tax_rates),
                )
                for line in lines
            ]
        )
    except ValueError as error:
        raise ValidationError(str(error)) from error
    if not invoice.currency:
        invoice.currency = Tenant.objects.get(pk=require_current_tenant_id()).default_currency
    invoice.status = Invoice.Status.DRAFT
    invoice.subtotal, invoice.tax_total, invoice.total = (
        totals.subtotal,
        totals.tax_total,
        totals.total,
    )
    invoice.series = None
    invoice.number = invoice.year = invoice.issuer = invoice.customer_details = None
    invoice.issued_at = None
    invoice.reference = ""
    _clean(invoice)
    invoice.save()
    invoice.lines.all().delete()
    for position, (draft, amounts) in enumerate(zip(lines, totals.lines, strict=True), 1):
        row = InvoiceLine(
            invoice=invoice,
            position=position,
            product=draft.product,
            description=draft.description,
            quantity=draft.quantity,
            unit_price=draft.unit_price,
            discount_bp=draft.discount_bp,
            net=amounts.net,
            total=amounts.total,
        )
        _clean(row)
        row.save()
        for rate, amount in zip(draft.tax_rates, amounts.taxes, strict=True):
            tax = InvoiceLineTax(
                line=row, tax_rate=rate, name=rate.name, rate_bp=rate.rate_bp, amount=amount
            )
            _clean(tax)
            tax.save()
    action = "invoice.updated" if stored else "invoice.created"
    record_event(principal, action, "invoice", invoice.pk, before, _snapshot(invoice))
    return invoice


@transaction.atomic
def delete_draft(principal: Principal | None, invoice: Invoice) -> None:
    """Delete a draft invoice with its lines, recording the state it had.

    An invoice that is not stored for the current tenant is left alone. InvalidTransition is
    raised when the stored invoice is not a draft.

    Args:
        principal: Acting principal, who needs invoices:write or PermissionDenied is raised.
        invoice: Draft invoice to remove.
    """
    require_permission(principal, Permission.INVOICES_WRITE)
    stored = _stored_draft(invoice)
    if stored is None:
        return
    record_event(principal, "invoice.deleted", "invoice", stored.pk, _snapshot(stored))
    stored.delete()


PAYMENT_TERM_DAYS = 30
"""Days after the issue date that an invoice falls due when its draft has no usable due date."""

DEFAULT_PREFIXES = {Invoice.Kind.INVOICE: "INV-", Invoice.Kind.CREDIT_NOTE: "CN-"}
"""Prefix of the series created for each kind the first time a document of it is issued."""

REQUIRED_ISSUER_FIELDS = ["legal_name", "address_line1", "city", "state", "postal_code"]
"""Tenant columns that must be filled in before an invoice can be issued."""


def _lock_series(kind: str, local_year: int) -> InvoiceSeries:
    """Create the current tenant's series for a kind if needed, and lock it.

    The lock is held until the surrounding transaction ends, so concurrent issues queue up behind
    each other. The year given only sets the year of a series created now.

    Args:
        kind: Kind of document being numbered.
        local_year: Year of the tenant-local date at the moment of the call, used for a new series.

    Returns:
        The locked series.
    """
    created, _ = InvoiceSeries.objects.get_or_create(
        kind=kind, defaults={"prefix": DEFAULT_PREFIXES[Invoice.Kind(kind)], "year": local_year}
    )
    locked: InvoiceSeries = InvoiceSeries.objects.select_for_update().get(pk=created.pk)
    return locked


def _lock_original(note: Invoice) -> Invoice:
    """Lock the original of a credit note and check that this note may still be issued.

    InvalidTransition is raised when the original can no longer be credited.

    Args:
        note: Draft credit note about to be issued.

    Returns:
        The locked original.

    Raises:
        ValidationError: When the credits would exceed the total of the original.
    """
    original: Invoice = Invoice.objects.select_for_update().get(pk=note.original_id)
    _require_creditable(original)
    if original.credited_total() + note.total > original.total:
        raise ValidationError("The credit notes would exceed the total of the original invoice.")
    return original


@transaction.atomic
def issue_invoice(principal: Principal | None, invoice: Invoice) -> Invoice:
    """Issue a draft: number it, freeze the issuer and customer details and set its dates.

    The series is locked first and the clock is read afterwards, so a request that waited for
    the lock never numbers with a date older than the series. InvalidTransition is also raised
    when the issue year is earlier than the year of the series, which only a clock behind
    another server can cause; nothing changes then. The issue date is today in the timezone of
    the tenant, and the due date is kept when it is
    not before the issue date, otherwise it is PAYMENT_TERM_DAYS days later. The invoice
    receives the next number of its series and a reference such as INV-2026-00001, which is
    stored so that a later prefix change never alters it. InvalidTransition is raised when the
    invoice is not a draft, and the event invoice.issued is recorded in the audit log. The
    invoice row is locked, so issuing and editing the same draft are serialized.

    Issuing a credit note also locks its original, before the series. ValidationError is raised
    when the issued credit notes of the original, this one included, would exceed its total, and
    InvalidTransition when the original can no longer be credited. When the credits reach the
    total exactly, the original becomes voided and invoice.voided is recorded, all in the same
    transaction. When the credits are below the total but payments cover the rest, so nothing
    is owed any more, the original becomes paid and invoice.paid is recorded instead.

    Once the transaction commits, the task render_invoice_pdf is enqueued to render the PDF.

    Args:
        principal: Acting principal, who needs invoices:write or PermissionDenied is raised.
        invoice: Draft invoice to issue.

    Returns:
        The issued invoice.

    Raises:
        Invoice.DoesNotExist: When the invoice is not stored for the current tenant.
        InvalidTransition: When the invoice is not a draft or the clock is behind the series.
        ValidationError: When the invoice has no lines or the issuer details are incomplete.
    """
    require_permission(principal, Permission.INVOICES_WRITE)
    stored = _stored_draft(invoice)
    if stored is None:
        raise Invoice.DoesNotExist("invoice is not stored for the current tenant")
    if not stored.lines.exists():
        raise ValidationError("An invoice needs at least one line to be issued.")
    tenant = Tenant.objects.get(pk=require_current_tenant_id())
    missing = [name for name in REQUIRED_ISSUER_FIELDS if not getattr(tenant, name)]
    if missing:
        raise ValidationError(f"The issuer details are incomplete: {', '.join(missing)}.")
    original, before_status = None, ""
    if stored.kind == Invoice.Kind.CREDIT_NOTE:
        # Locked before the series, so credits of one invoice queue up on it first.
        original = _lock_original(stored)
        before_status = original.status
    before = _snapshot(stored)
    series = _lock_series(stored.kind, _local_date(tenant, timezone.now()).year)
    now = timezone.now()
    today = _local_date(tenant, now)
    if today.year < series.year:
        raise InvalidTransition(
            f"the clock is in {today.year} but the series is already in {series.year}"
        )
    if today.year > series.year:
        series.year, series.next_number = today.year, 1
    number = series.next_number
    series.next_number = number + 1
    series.save(update_fields=["year", "next_number"])
    reference = f"{series.prefix}{series.year}-{number:05d}"
    customer = stored.customer
    stored.series, stored.number, stored.year, stored.reference = (
        series,
        number,
        series.year,
        reference,
    )
    stored.issuer = {
        "legal_name": tenant.legal_name,
        "ein": tenant.ein,
        "address_line1": tenant.address_line1,
        "address_line2": tenant.address_line2,
        "city": tenant.city,
        "state": tenant.state,
        "postal_code": tenant.postal_code,
        "country": tenant.country,
        "currency": tenant.default_currency,
    }
    stored.customer_details = {
        "name": customer.name,
        "tax_id": customer.tax_id,
        "emails": customer.emails,
        "address_line1": customer.address_line1,
        "address_line2": customer.address_line2,
        "city": customer.city,
        "state": customer.state,
        "postal_code": customer.postal_code,
        "country": customer.country,
    }
    stored.issued_at, stored.issue_date = now, today
    if stored.due_date is None or stored.due_date < today:
        stored.due_date = today + timedelta(days=PAYMENT_TERM_DAYS)
    stored.status = Invoice.Status.ISSUED
    stored.save()
    after = _snapshot(stored) | {"reference": reference}
    record_event(principal, "invoice.issued", "invoice", stored.pk, before, after)
    if original is not None and original.credited_total() == original.total:
        original.status = Invoice.Status.VOIDED
        original.save(update_fields=["status", "updated_at"])
        record_event(
            principal,
            "invoice.voided",
            "invoice",
            original.pk,
            {"status": before_status},
            {"status": original.status},
        )
    elif (
        original is not None
        and original.balance_due() <= 0
        and before_status != Invoice.Status.PAID
    ):
        original.status = Invoice.Status.PAID
        original.save(update_fields=["status", "updated_at"])
        record_event(
            principal,
            "invoice.paid",
            "invoice",
            original.pk,
            {"status": before_status},
            {"status": original.status},
        )
    # Imported here because the task module imports this one.
    from billing.tasks import render_invoice_pdf

    transaction.on_commit(lambda: render_invoice_pdf.delay(str(stored.tenant_id), str(stored.pk)))
    return stored


CREDITABLE_STATUSES = (
    Invoice.Status.ISSUED,
    Invoice.Status.SENT,
    Invoice.Status.PARTIALLY_PAID,
    Invoice.Status.PAID,
)
"""Statuses of an invoice that can still be corrected with a credit note."""


def _require_creditable(original: Invoice) -> None:
    """Refuse an original that cannot be credited, which includes a voided one.

    Args:
        original: Stored invoice to check.

    Raises:
        InvalidTransition: When it is not an invoice in a creditable status.
    """
    if original.kind != Invoice.Kind.INVOICE:
        raise InvalidTransition("only an invoice can be credited, not a credit note")
    if original.status not in CREDITABLE_STATUSES:
        raise InvalidTransition(f"invoice is {original.status}, it cannot be credited")


@transaction.atomic
def draft_credit_note(
    principal: Principal | None, original: Invoice, lines: list[DraftLine] | None = None
) -> Invoice:
    """Draft a credit note for an issued invoice, for the full amount or a part of it.

    The credit note is a draft of the same customer and currency, saved through save_draft, so
    it is issued like any draft and numbered in the credit note series. Amounts are stored
    positive and the kind gives the sign. Without lines it is a full credit: the lines of the
    original are copied, each tax with the name and rate it had when the original was drafted,
    so the total equals the original total even if a tax rate has been edited since. With
    lines it is a partial credit. InvalidTransition is raised when the original is not an
    invoice in the issued, sent, partially paid or paid status, so a voided invoice cannot be
    credited again.

    Args:
        principal: Acting principal, who needs invoices:write or PermissionDenied is raised.
        original: Invoice to credit.
        lines: Lines of a partial credit, or None to credit the whole original.

    Returns:
        The draft credit note.

    Raises:
        Invoice.DoesNotExist: When the invoice is not stored for the current tenant.
    """
    require_permission(principal, Permission.INVOICES_WRITE)
    stored = Invoice.objects.filter(pk=original.pk).first()
    if stored is None:
        raise Invoice.DoesNotExist("invoice is not stored for the current tenant")
    _require_creditable(stored)
    if lines is None:
        lines = [
            DraftLine(
                line.description,
                line.quantity,
                line.unit_price,
                line.discount_bp,
                # An unsaved rate carrying the snapshot: save_draft reads its name and rate and
                # links to it by key, so no schema or save_draft change is needed.
                tuple(
                    TaxRate(pk=tax.tax_rate_id, name=tax.name, rate_bp=tax.rate_bp)
                    for tax in line.taxes.order_by("id")
                ),
                line.product,
            )
            for line in stored.lines.order_by("position")
        ]
    note = Invoice(
        kind=Invoice.Kind.CREDIT_NOTE,
        original=stored,
        customer=stored.customer,
        currency=stored.currency,
    )
    return save_draft(principal, note, lines)


def _local_date(tenant: Tenant, moment: datetime) -> date:
    """Convert a moment to the calendar day it is in the timezone of a tenant.

    Args:
        tenant: Tenant whose timezone decides the day.
        moment: Timezone-aware moment.

    Returns:
        The local date.
    """
    return moment.astimezone(zoneinfo.ZoneInfo(tenant.timezone)).date()


def save_series(principal: Principal | None, series: InvoiceSeries) -> InvoiceSeries:
    """Create an invoice series, or change the prefix of an existing one.

    A new series starts at number 1 in the current year of the tenant timezone. For an existing
    series only the prefix is written, so the counter and the year can never be edited; the
    stored counter, year and kind replace whatever the instance carried. ValidationError is
    raised for invalid data, including a second series of a kind the tenant already has.

    Args:
        principal: Acting principal, who needs settings:manage or PermissionDenied is raised.
        series: Series to persist.

    Returns:
        The saved series, carrying the stored counter.
    """
    require_permission(principal, Permission.SETTINGS_MANAGE)
    if series.pk is None:
        tenant = Tenant.objects.get(pk=require_current_tenant_id())
        series.year, series.next_number = _local_date(tenant, timezone.now()).year, 1
        _clean(series)
        series.save()
    else:
        series.refresh_from_db(fields=["kind", "year", "next_number"])
        _clean(series)
        series.save(update_fields=["prefix"])
    return series


def delete_series(principal: Principal | None, series: InvoiceSeries) -> None:
    """Delete an invoice series.

    A series that numbered an invoice cannot be deleted, because the invoices protect it, and
    Django raises ProtectedError.

    Args:
        principal: Acting principal, who needs settings:manage or PermissionDenied is raised.
        series: Series to remove.
    """
    require_permission(principal, Permission.SETTINGS_MANAGE)
    series.delete()


@transaction.atomic
def record_payment(
    principal: Principal | None, payment: Payment, allocations: list[tuple[Invoice, int]]
) -> Payment:
    """Record a payment and apply it to invoices, setting each invoice paid or partially paid.

    The invoices are locked in primary key order, so concurrent payments cannot deadlock, and
    their balances are read after the lock, so two payments never both use the same balance.
    Nothing is stored unless every check passes. A part of the payment that is not allocated
    stays unallocated, for reconciliation to use later. For each invoice the event
    invoice.payment_recorded is recorded with its status and balance before and after.

    Args:
        principal: Acting principal, who needs invoices:write or PermissionDenied is raised.
        payment: Payment to store, which is validated first.
        allocations: Pairs of an invoice and the cents of the payment applied to it.

    Returns:
        The saved payment.

    Raises:
        Invoice.DoesNotExist: When an invoice is not stored for the current tenant.
        InvalidTransition: When the payment is already recorded, so a recorded amount is never
            rewritten, or an invoice is not an invoice in the issued, sent or partially paid
            status.
        ValidationError: When an invoice appears twice or is in another currency than the
            payment, an amount is not positive or exceeds the balance due of its invoice, the
            allocations together exceed the payment, or the payment itself is invalid.
    """
    require_permission(principal, Permission.INVOICES_WRITE)
    if not payment._state.adding:
        raise InvalidTransition("a recorded payment cannot be recorded again")
    _clean(payment)
    ids = [invoice.pk for invoice, _ in allocations]
    if len(set(ids)) != len(ids):
        raise ValidationError("An invoice can be allocated only once per payment.")
    locked = {
        row.pk: row for row in Invoice.objects.select_for_update().filter(pk__in=ids).order_by("pk")
    }
    if len(locked) != len(ids):
        raise Invoice.DoesNotExist("an invoice is not stored for the current tenant")
    if sum(cents for _, cents in allocations) > payment.amount:
        raise ValidationError("The allocations exceed the amount of the payment.")
    plan: list[tuple[Invoice, int, int]] = []
    for invoice, cents in sorted(allocations, key=lambda pair: pair[0].pk):
        row = locked[invoice.pk]
        if row.kind != Invoice.Kind.INVOICE or row.status not in Invoice.PAYABLE_STATUSES:
            raise InvalidTransition(f"{row.kind} is {row.status}, it cannot be paid")
        if row.currency != payment.currency:
            raise ValidationError("The payment and the invoice are in different currencies.")
        balance = row.balance_due()
        if not 0 < cents <= balance:
            raise ValidationError(
                f"An allocation must be positive and at most the balance due of {balance}."
            )
        plan.append((row, cents, balance))
    payment.save()
    for row, cents, balance in plan:
        allocation = PaymentAllocation(payment=payment, invoice=row, amount=cents)
        _clean(allocation)
        allocation.save()
        before_status = row.status
        remaining = balance - cents
        row.status = Invoice.Status.PAID if remaining == 0 else Invoice.Status.PARTIALLY_PAID
        if row.status != before_status:
            _clean(row)
            row.save(update_fields=["status", "updated_at"])
        record_event(
            principal,
            "invoice.payment_recorded",
            "invoice",
            row.pk,
            {"status": before_status, "balance": balance},
            {"status": row.status, "balance": remaining, "payment_id": payment.pk},
        )
    return payment


def _payment_snapshot(payment: Payment) -> dict[str, Any]:
    """Describe a payment and its allocations for an audit event.

    Args:
        payment: Stored payment to describe.

    Returns:
        Its id, amount, currency, value date as an ISO string, and the allocations as invoice id
        and amount, by invoice id.
    """
    allocations = payment.allocations.order_by("invoice_id")
    return {
        "id": payment.pk,
        "amount": payment.amount,
        "currency": payment.currency,
        "value_date": payment.value_date.isoformat(),
        "allocations": [
            {"invoice_id": str(row.invoice_id), "amount": row.amount} for row in allocations
        ],
    }


@transaction.atomic
def reverse_payment(principal: Principal | None, payment: Payment) -> None:
    """Undo a recorded payment: delete it and put its invoices back to what is still paid.

    The payment row is locked, so a payment of another tenant, which Row-Level Security hides,
    raises Payment.DoesNotExist. Its invoices are locked in primary key order, as record_payment
    does. Each must be paid or partially paid: a voided or credited invoice is never silently
    reopened. The payment and its allocations are deleted, and bank transactions that pointed at
    the payment lose the link. Each invoice is then partially paid when other allocations remain,
    otherwise sent when an email of it was delivered (sent_at is set), otherwise issued. The
    status follows the balance first: an invoice that the remaining payments and credit notes
    still settle stays paid. For each invoice the event invoice.payment_reversed records its
    status and balance before and after, and the deleted payment with its allocations in the
    before state. A payment with no allocations produces no invoice event, so the caller is
    responsible for auditing it. Everything is one transaction, so a refusal writes nothing.

    ponytail: deleting the payment keeps its history only in the audit log. If accounting needs
    the payment row kept, upgrade to a reversal record instead of a delete.

    Args:
        principal: Acting principal, who needs invoices:write or PermissionDenied is raised.
        payment: Payment to reverse, read again under lock.

    Raises:
        InvalidTransition: When an invoice of the payment is not paid or partially paid.
    """
    require_permission(principal, Permission.INVOICES_WRITE)
    locked = Payment.objects.select_for_update().get(pk=payment.pk)
    payment_id = locked.pk
    ids = list(locked.allocations.values_list("invoice_id", flat=True))
    rows = list(Invoice.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
    paid = (Invoice.Status.PAID, Invoice.Status.PARTIALLY_PAID)
    for row in rows:
        if row.status not in paid:
            raise InvalidTransition(f"{row.kind} is {row.status}, its payment cannot be reversed")
    before = {row.pk: (row.status, row.balance_due()) for row in rows}
    snapshot = _payment_snapshot(locked)
    locked.delete()
    for row in rows:
        balance = row.balance_due()
        if balance <= 0:
            row.status = Invoice.Status.PAID
        elif row.allocations.exists():
            row.status = Invoice.Status.PARTIALLY_PAID
        elif InvoiceEmail.objects.filter(invoice=row, sent_at__isnull=False).exists():
            row.status = Invoice.Status.SENT
        else:
            row.status = Invoice.Status.ISSUED
        if row.status != before[row.pk][0]:
            _clean(row)
            row.save(update_fields=["status", "updated_at"])
        record_event(
            principal,
            "invoice.payment_reversed",
            "invoice",
            row.pk,
            {"status": before[row.pk][0], "balance": before[row.pk][1], "payment": snapshot},
            {"status": row.status, "balance": balance, "payment_id": payment_id},
        )


@transaction.atomic
def send_invoice(
    principal: Principal | None,
    invoice: Invoice,
    idempotency_key: str,
    recipients: list[str] | None = None,
) -> InvoiceEmail:
    """Request that an invoice is emailed with its PDF, once per idempotency key.

    The invoice row is locked, so requests for the same invoice are serialized. A key already
    used returns its request without storing, enqueueing or auditing anything again, even if
    the invoice has changed status since. A new request is validated and stored, the event
    invoice.send_requested is recorded with the recipients, and once the transaction commits
    the task send_invoice_email is enqueued to deliver it.

    Args:
        principal: Acting principal, who needs invoices:write or PermissionDenied is raised.
        invoice: Invoice to send.
        idempotency_key: Caller-chosen key that identifies this request within the tenant.
        recipients: Addresses to send to; the emails of the customer snapshot when omitted.

    Returns:
        The stored request, which is the earlier one when the key was already used.

    Raises:
        Invoice.DoesNotExist: When the invoice is not stored for the current tenant.
        InvalidTransition: When the invoice is a draft or voided.
        ValidationError: When the key belongs to another invoice, there is no recipient or an
            address is invalid.
    """
    require_permission(principal, Permission.INVOICES_WRITE)
    stored = Invoice.objects.select_for_update().filter(pk=invoice.pk).first()
    if stored is None:
        raise Invoice.DoesNotExist("invoice is not stored for the current tenant")
    existing: InvoiceEmail | None = InvoiceEmail.objects.filter(
        idempotency_key=idempotency_key
    ).first()
    if existing is not None:
        if existing.invoice_id != stored.pk:
            raise ValidationError("This idempotency key was used for another invoice.")
        return existing
    if stored.status in (Invoice.Status.DRAFT, Invoice.Status.VOIDED):
        raise InvalidTransition(f"invoice is {stored.status}, only an issued invoice is sent")
    if recipients is None:
        recipients = (stored.customer_details or {}).get("emails", [])
    if not recipients:
        raise ValidationError("An invoice needs at least one recipient to be sent.")
    request = InvoiceEmail(invoice=stored, idempotency_key=idempotency_key, recipients=recipients)
    _clean(request)
    request.save()
    record_event(
        principal,
        "invoice.send_requested",
        "invoice",
        stored.pk,
        after={"recipients": recipients, "idempotency_key": idempotency_key},
    )
    # Imported here because the task module imports this one.
    from billing.tasks import send_invoice_email

    transaction.on_commit(lambda: send_invoice_email.delay(str(request.tenant_id), str(request.pk)))
    return request
