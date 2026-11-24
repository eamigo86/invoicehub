"""Issued invoices are frozen in the database, whatever path writes to them."""

from collections.abc import Callable, Generator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from django.db import IntegrityError, connection
from django.utils import timezone

from billing.models import Customer, Invoice, InvoiceLine, InvoiceLineTax, TaxRate
from billing.services import DraftLine, delete_draft, issue_invoice, save_draft
from tenancy.context import tenant_context
from tenancy.models import Tenant
from tenancy.permissions import Permission, Principal

pytestmark = pytest.mark.django_db(transaction=True)


@dataclass
class World:
    """A committed tenant with one issued invoice and one draft, each with a taxed line.

    Attributes:
        tenant: Tenant that owns every row.
        writer: Principal allowed to write invoices.
        issued: The issued invoice.
        issued_line: Line of the issued invoice.
        issued_tax: Tax of that line.
        draft: The draft invoice.
        draft_line: Line of the draft.
    """

    tenant: Tenant
    writer: Principal
    issued: Invoice
    issued_line: InvoiceLine
    issued_tax: InvoiceLineTax
    draft: Invoice
    draft_line: InvoiceLine


@pytest.fixture
def world() -> Generator[World]:
    """Commit a tenant with an issued invoice and a draft, using the services.

    Yields:
        The populated world.
    """
    tenant = Tenant.objects.create(
        name="A",
        legal_name="Acme Inc.",
        address_line1="1 Main St",
        city="Austin",
        state="TX",
        postal_code="78701",
    )
    writer = Principal(tenant.pk, frozenset({Permission.INVOICES_WRITE}))
    with tenant_context(tenant.pk):
        customer = Customer.objects.create(name="Bolt")
        rate = TaxRate.objects.create(name="Sales", rate_bp=825)
        lines = [DraftLine("Widget", Decimal("1"), 1000, tax_rates=(rate,))]
        issued = issue_invoice(writer, save_draft(writer, Invoice(customer=customer), lines))
        draft = save_draft(writer, Invoice(customer=customer), lines)
        yield World(
            tenant,
            writer,
            issued,
            issued.lines.get(),
            InvoiceLineTax.objects.get(line__invoice=issued),
            draft,
            draft.lines.get(),
        )


def refused(world: World, action: Callable[[], object]) -> None:
    """Run an action in its own transaction and require the database to refuse it.

    Args:
        world: World whose tenant is activated.
        action: Write that must raise IntegrityError.
    """
    with (
        pytest.raises(IntegrityError, match="issued invoices are immutable"),
        tenant_context(world.tenant.pk),
    ):
        action()


def raw(sql: str, *params: Any) -> Callable[[], None]:
    """Build an action that runs one raw SQL statement.

    Args:
        sql: Statement to execute.
        *params: Values for its placeholders.

    Returns:
        A function that executes the statement.
    """

    def run() -> None:
        """Execute the statement on the default connection."""
        with connection.cursor() as cursor:
            cursor.execute(sql, params)

    return run


def test_issued_invoice_columns_cannot_change(world: World) -> None:
    """The ORM and raw SQL cannot change any frozen column of an issued invoice.

    Args:
        world: Committed tenant with issued and draft invoices.
    """
    pk = world.issued.pk
    refused(world, lambda: Invoice.objects.filter(pk=pk).update(notes="edited"))
    refused(world, lambda: Invoice.objects.filter(pk=pk).update(total=1))
    refused(world, raw("UPDATE billing_invoice SET total = 1 WHERE id = %s", pk))
    refused(
        world, raw("UPDATE billing_invoice SET status = 'draft', notes = 'x' WHERE id = %s", pk)
    )
    refused(world, raw("UPDATE billing_invoice SET status = 'draft' WHERE id = %s", pk))
    with tenant_context(world.tenant.pk):
        stored = Invoice.objects.get(pk=pk)
    assert (stored.notes, stored.total) == ("", world.issued.total)


def test_issued_invoice_cannot_be_deleted(world: World) -> None:
    """Neither the ORM nor raw SQL deletes an issued invoice, and its lines stay.

    Args:
        world: Committed tenant with issued and draft invoices.
    """
    pk = world.issued.pk
    refused(world, lambda: Invoice.objects.filter(pk=pk).delete())
    refused(world, raw("DELETE FROM billing_invoice WHERE id = %s", pk))
    with tenant_context(world.tenant.pk):
        assert Invoice.objects.filter(pk=pk).exists()
        assert InvoiceLine.objects.filter(invoice_id=pk).count() == 1


def test_lines_and_taxes_of_an_issued_invoice_are_frozen(world: World) -> None:
    """Inserting, updating or deleting a line or line tax of an issued invoice is refused.

    Args:
        world: Committed tenant with issued and draft invoices.
    """
    line, tax = world.issued_line, world.issued_tax

    def insert_line() -> None:
        """Insert a second line into the issued invoice with bulk_create."""
        InvoiceLine.objects.bulk_create(
            [
                InvoiceLine(
                    tenant_id=world.tenant.pk,
                    invoice=world.issued,
                    position=2,
                    description="Extra",
                    quantity=1,
                    unit_price=1,
                )
            ]
        )

    def insert_tax() -> None:
        """Insert a second tax into the line of the issued invoice."""
        InvoiceLineTax.objects.create(
            tenant_id=world.tenant.pk, line=line, name="Extra", rate_bp=1, amount=1
        )

    refused(world, insert_line)
    refused(world, insert_tax)
    refused(world, lambda: InvoiceLine.objects.filter(pk=line.pk).update(unit_price=1))
    refused(world, lambda: InvoiceLine.objects.filter(pk=line.pk).delete())
    refused(world, lambda: InvoiceLineTax.objects.filter(pk=tax.pk).update(amount=1))
    refused(world, lambda: InvoiceLineTax.objects.filter(pk=tax.pk).delete())
    refused(world, raw("DELETE FROM billing_invoiceline WHERE id = %s", line.pk))
    with tenant_context(world.tenant.pk):
        assert InvoiceLine.objects.filter(invoice=world.issued).count() == 1
        assert InvoiceLineTax.objects.get(pk=tax.pk).amount == tax.amount
        assert InvoiceLine.objects.get(pk=line.pk).unit_price == line.unit_price


def test_a_line_cannot_move_into_or_out_of_an_issued_invoice(world: World) -> None:
    """Re-parenting a line is refused when either the old or the new invoice is issued.

    Args:
        world: Committed tenant with issued and draft invoices.
    """
    into = raw(
        "UPDATE billing_invoiceline SET invoice_id = %s, position = 9 WHERE id = %s",
        world.issued.pk,
        world.draft_line.pk,
    )
    out_of = raw(
        "UPDATE billing_invoiceline SET invoice_id = %s, position = 9 WHERE id = %s",
        world.draft.pk,
        world.issued_line.pk,
    )
    refused(world, into)
    refused(world, out_of)


def test_the_status_of_an_issued_invoice_can_change(world: World) -> None:
    """Status and updated_at are the only columns allowed to change after issue.

    Args:
        world: Committed tenant with issued and draft invoices.
    """
    with tenant_context(world.tenant.pk):
        Invoice.objects.filter(pk=world.issued.pk).update(
            status=Invoice.Status.SENT, updated_at=timezone.now()
        )
        assert Invoice.objects.get(pk=world.issued.pk).status == Invoice.Status.SENT


def test_a_draft_stays_fully_editable(world: World) -> None:
    """Saving a draft again replaces its lines, and a draft can be deleted.

    Args:
        world: Committed tenant with issued and draft invoices.
    """
    with tenant_context(world.tenant.pk):
        draft = Invoice.objects.get(pk=world.draft.pk)
        draft.notes = "changed"
        save_draft(world.writer, draft, [DraftLine("Other", Decimal("2"), 500)])
        assert [line.description for line in draft.lines.all()] == ["Other"]
        delete_draft(world.writer, draft)
        assert not Invoice.objects.filter(pk=world.draft.pk).exists()


def test_a_line_whose_invoice_is_invisible_is_refused(world: World) -> None:
    """A line insert fails closed when the parent invoice cannot be seen.

    Args:
        world: Committed tenant with issued and draft invoices.
    """
    other = Tenant.objects.create(name="B")
    with (
        pytest.raises(IntegrityError, match="issued invoices are immutable"),
        tenant_context(other.pk),
    ):
        InvoiceLine.objects.create(
            tenant_id=other.pk,
            invoice=world.draft,
            position=5,
            description="Foreign",
            quantity=1,
            unit_price=1,
        )
