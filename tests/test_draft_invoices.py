"""Draft invoices: stored totals, tax snapshots, line replacement, audit and permissions."""

from collections.abc import Generator
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from accounts.models import User
from billing.calculations import LineInput, TaxRateInput, invoice_totals
from billing.models import (
    AuditEvent,
    Customer,
    Invoice,
    InvoiceLine,
    InvoiceLineTax,
    Product,
    TaxRate,
)
from billing.services import DraftLine, InvalidTransition, delete_draft, save_draft
from core.models import Currency
from tenancy.context import tenant_context
from tenancy.models import Tenant
from tenancy.permissions import Permission, Principal

pytestmark = pytest.mark.django_db


@pytest.fixture
def tenant() -> Generator[Tenant]:
    """Create a tenant and make it the active one for the test.

    Yields:
        The tenant.
    """
    tenant = Tenant.objects.create(name="A")
    with tenant_context(tenant.pk):
        yield tenant


@pytest.fixture
def user() -> User:
    """Create the user behind the writer principal.

    Returns:
        The user.
    """
    return User.objects.create_user("ann", "ann@example.com", "pw")


@pytest.fixture
def writer(tenant: Tenant, user: User) -> Principal:
    """Build a principal that may write invoices, acting as the user.

    Args:
        tenant: Active tenant.
        user: User the principal stands for.

    Returns:
        The principal.
    """
    permissions = frozenset({Permission.INVOICES_READ, Permission.INVOICES_WRITE})
    return Principal(tenant.pk, permissions, user_id=user.pk)


@pytest.fixture
def customer(tenant: Tenant) -> Customer:
    """Create a customer in the active tenant.

    Args:
        tenant: Active tenant.

    Returns:
        The customer.
    """
    created: Customer = Customer.objects.create(name="Acme")
    return created


def two_lines(austin: TaxRate, county: TaxRate) -> list[DraftLine]:
    """Build a multi-line draft input with several rates and a discount.

    Args:
        austin: Rate applied to both lines.
        county: Rate applied to the second line only.

    Returns:
        The lines.
    """
    return [
        DraftLine("Widget", Decimal("3"), 1999, tax_rates=(austin,)),
        DraftLine("Service", Decimal("1.5"), 4999, 1250, (austin, county)),
    ]


def test_stored_totals_equal_the_calculation(writer: Principal, customer: Customer) -> None:
    """Line and invoice amounts, and each line tax, are what invoice_totals computes.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    austin = TaxRate.objects.create(name="Austin", rate_bp=825)
    county = TaxRate.objects.create(name="County", rate_bp=250)
    expected = invoice_totals(
        [
            LineInput(Decimal("3"), 1999, 0, (TaxRateInput("Austin", 825),)),
            LineInput(
                Decimal("1.5"),
                4999,
                1250,
                (TaxRateInput("Austin", 825), TaxRateInput("County", 250)),
            ),
        ]
    )
    saved = save_draft(writer, Invoice(customer=customer), two_lines(austin, county))
    stored = Invoice.objects.get(pk=saved.pk)
    assert (stored.subtotal, stored.tax_total, stored.total) == (
        expected.subtotal,
        expected.tax_total,
        expected.total,
    )
    assert stored.total > 0 and stored.tax_total > 0
    lines = list(stored.lines.all())
    assert [(line.position, line.net, line.total) for line in lines] == [
        (index, amounts.net, amounts.total) for index, amounts in enumerate(expected.lines, 1)
    ]
    assert [[tax.amount for tax in line.taxes.order_by("id")] for line in lines] == [
        list(amounts.taxes) for amounts in expected.lines
    ]
    assert stored.status == Invoice.Status.DRAFT


def test_line_taxes_keep_their_snapshot_when_the_rate_is_edited(
    writer: Principal, customer: Customer
) -> None:
    """Editing the configured rate afterwards changes neither the name nor the rate stored.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    austin = TaxRate.objects.create(name="Austin", rate_bp=825)
    saved = save_draft(
        writer, Invoice(customer=customer), [DraftLine("Widget", Decimal("1"), 1000, 0, (austin,))]
    )
    austin.name = "Renamed"
    austin.rate_bp = 1
    austin.save()
    tax = InvoiceLineTax.objects.get(line__invoice=saved)
    assert (tax.name, tax.rate_bp, tax.amount, tax.tax_rate) == ("Austin", 825, 83, austin)


def test_a_line_keeps_its_product(writer: Principal, customer: Customer) -> None:
    """A line created from a product references it.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    product = Product.objects.create(name="Widget", unit_price=1000)
    saved = save_draft(
        writer,
        Invoice(customer=customer),
        [DraftLine("Widget", Decimal("1"), 1000, product=product)],
    )
    assert saved.lines.get().product == product


def test_saving_again_replaces_the_lines_and_recomputes_the_totals(
    writer: Principal, customer: Customer
) -> None:
    """The second save leaves only the new lines and their taxes, and updates the totals.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    austin = TaxRate.objects.create(name="Austin", rate_bp=1000)
    saved = save_draft(
        writer, Invoice(customer=customer), [DraftLine("Old", Decimal("1"), 1000, 0, (austin,))]
    )
    saved.notes = "revised"
    save_draft(
        writer, saved, [DraftLine("New", Decimal("2"), 500), DraftLine("Newer", Decimal("1"), 100)]
    )
    stored = Invoice.objects.get(pk=saved.pk)
    assert [(line.position, line.description) for line in stored.lines.all()] == [
        (1, "New"),
        (2, "Newer"),
    ]
    assert (stored.subtotal, stored.tax_total, stored.total, stored.notes) == (
        1100,
        0,
        1100,
        "revised",
    )
    assert (InvoiceLine.objects.count(), InvoiceLineTax.objects.count()) == (2, 0)


def test_a_new_draft_gets_the_default_currency_of_the_tenant(
    tenant: Tenant, writer: Principal, customer: Customer
) -> None:
    """With no currency set the tenant default applies, and an explicit one is kept.

    Args:
        tenant: Active tenant.
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    tenant.default_currency = Currency.CAD
    tenant.save()
    defaulted = save_draft(writer, Invoice(customer=customer), [])
    explicit = save_draft(writer, Invoice(customer=customer, currency=Currency.MXN), [])
    assert (defaulted.currency, explicit.currency) == (Currency.CAD, Currency.MXN)


def test_a_caller_cannot_save_a_new_invoice_as_issued(
    writer: Principal, customer: Customer
) -> None:
    """Whatever status the instance carries, a saved draft is a draft.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    saved = save_draft(writer, Invoice(customer=customer, status=Invoice.Status.ISSUED), [])
    assert Invoice.objects.get(pk=saved.pk).status == Invoice.Status.DRAFT


@pytest.mark.parametrize(
    "line",
    [
        DraftLine("Bad", Decimal("-1"), 100),
        DraftLine("Bad", Decimal("1.0001"), 100),
        DraftLine("Bad", Decimal("1"), -1),
        DraftLine("Bad", Decimal("1"), 100, 10001),
    ],
)
def test_an_invalid_line_stores_nothing(
    writer: Principal, customer: Customer, line: DraftLine
) -> None:
    """A line out of range raises ValidationError and neither invoice nor lines are stored.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
        line: Invalid line.
    """
    with pytest.raises(ValidationError):
        save_draft(writer, Invoice(customer=customer), [DraftLine("Fine", Decimal("1"), 1), line])
    assert (Invoice.objects.count(), InvoiceLine.objects.count()) == (0, 0)
    assert AuditEvent.objects.count() == 0


def test_an_invoice_without_a_customer_is_refused(writer: Principal) -> None:
    """The invoice is validated, so a missing customer raises ValidationError.

    Args:
        writer: Principal allowed to write invoices.
    """
    with pytest.raises(ValidationError):
        save_draft(writer, Invoice(), [])
    assert Invoice.objects.count() == 0


def test_the_audit_log_records_creation_and_update_with_the_acting_user(
    writer: Principal, user: User, customer: Customer
) -> None:
    """A created and an updated event carry the actor, the before state and a compact snapshot.

    Args:
        writer: Principal allowed to write invoices.
        user: User behind the principal.
        customer: Customer to invoice.
    """
    austin = TaxRate.objects.create(name="Austin", rate_bp=1000)
    saved = save_draft(
        writer, Invoice(customer=customer), [DraftLine("Old", Decimal("1"), 1000, 0, (austin,))]
    )
    save_draft(writer, saved, [DraftLine("New", Decimal("1"), 200)])
    created, updated = AuditEvent.objects.order_by("created_at", "id")
    assert (created.action, updated.action) == ("invoice.created", "invoice.updated")
    for event in (created, updated):
        assert (event.entity, event.entity_id) == ("invoice", saved.pk)
        assert (event.actor_kind, event.actor_user_id) == (AuditEvent.ActorKind.USER, user.pk)
    assert created.before is None
    assert created.after is not None
    assert (created.after["status"], created.after["total"]) == ("draft", 1100)
    assert created.after["customer_id"] == str(customer.pk)
    assert created.after["lines"] == [
        {
            "position": 1,
            "description": "Old",
            "quantity": "1.000",
            "unit_price": 1000,
            "discount_bp": 0,
            "product_id": None,
            "net": 1000,
            "total": 1100,
            "taxes": [{"name": "Austin", "rate_bp": 1000, "amount": 100}],
        }
    ]
    assert updated.before == created.after
    assert updated.after is not None
    assert updated.after["total"] == 200


def test_a_stored_non_draft_cannot_be_saved(writer: Principal, customer: Customer) -> None:
    """Once the stored invoice is issued, saving it is refused and nothing changes.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    saved = save_draft(writer, Invoice(customer=customer), [DraftLine("Old", Decimal("1"), 1000)])
    Invoice.objects.filter(pk=saved.pk).update(status=Invoice.Status.ISSUED)
    saved.status = Invoice.Status.DRAFT
    with pytest.raises(InvalidTransition):
        save_draft(writer, saved, [DraftLine("New", Decimal("1"), 1)])
    stored = Invoice.objects.get(pk=saved.pk)
    assert (stored.total, [line.description for line in stored.lines.all()]) == (1000, ["Old"])
    assert AuditEvent.objects.count() == 1


def test_a_viewer_cannot_save_a_draft(tenant: Tenant, customer: Customer) -> None:
    """A principal with only invoices:read is refused and nothing is stored.

    Args:
        tenant: Active tenant.
        customer: Customer to invoice.
    """
    viewer = Principal(tenant.pk, frozenset({Permission.INVOICES_READ}))
    with pytest.raises(PermissionDenied):
        save_draft(viewer, Invoice(customer=customer), [])
    with pytest.raises(PermissionDenied):
        save_draft(None, Invoice(customer=customer), [])
    assert Invoice.objects.count() == 0


def test_delete_draft_removes_the_invoice_and_audits_the_before_state(
    writer: Principal, customer: Customer
) -> None:
    """Deleting a draft removes its lines and records the snapshot it had.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    saved = save_draft(writer, Invoice(customer=customer), [DraftLine("Old", Decimal("1"), 1000)])
    delete_draft(writer, saved)
    assert (Invoice.objects.count(), InvoiceLine.objects.count()) == (0, 0)
    created, deleted = AuditEvent.objects.order_by("created_at", "id")
    assert (deleted.action, deleted.entity_id, deleted.after) == ("invoice.deleted", saved.pk, None)
    assert deleted.before == created.after


def test_a_non_draft_cannot_be_deleted(writer: Principal, customer: Customer) -> None:
    """Deleting an issued invoice is refused and the invoice stays.

    Args:
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    saved = save_draft(writer, Invoice(customer=customer), [])
    Invoice.objects.filter(pk=saved.pk).update(status=Invoice.Status.ISSUED)
    with pytest.raises(InvalidTransition):
        delete_draft(writer, saved)
    assert Invoice.objects.filter(pk=saved.pk).exists()
    assert AuditEvent.objects.count() == 1


def test_a_viewer_cannot_delete_a_draft(
    tenant: Tenant, writer: Principal, customer: Customer
) -> None:
    """A principal with only invoices:read is refused and the draft stays.

    Args:
        tenant: Active tenant.
        writer: Principal allowed to write invoices.
        customer: Customer to invoice.
    """
    saved = save_draft(writer, Invoice(customer=customer), [])
    viewer = Principal(tenant.pk, frozenset({Permission.INVOICES_READ}))
    with pytest.raises(PermissionDenied):
        delete_draft(viewer, saved)
    assert Invoice.objects.filter(pk=saved.pk).exists()
