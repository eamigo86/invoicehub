"""Issued invoices are frozen in the database, whatever path writes to them."""

import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from django.db import IntegrityError, connection, connections
from django.db.models import ProtectedError
from django.test import Client
from django.utils import timezone

from accounts.models import User
from billing.models import Customer, Invoice, InvoiceLine, InvoiceLineTax, Product, TaxRate
from billing.services import (
    DraftLine,
    delete_draft,
    delete_product,
    delete_tax_rate,
    issue_invoice,
    save_draft,
)
from tenancy.context import tenant_context
from tenancy.models import Membership, Tenant
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
    refused(world, raw("UPDATE billing_invoice SET id = gen_random_uuid() WHERE id = %s", pk))
    other = Tenant.objects.create(name="B")
    refused(world, raw("UPDATE billing_invoice SET tenant_id = %s WHERE id = %s", other.pk, pk))
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


def test_a_line_tax_cannot_move_into_or_out_of_an_issued_invoice(world: World) -> None:
    """Re-parenting a line tax is refused when either line belongs to an issued invoice.

    Args:
        world: Committed tenant with issued and draft invoices.
    """
    with tenant_context(world.tenant.pk):
        draft_tax = InvoiceLineTax.objects.get(line=world.draft_line)
    move = "UPDATE billing_invoicelinetax SET line_id = %s WHERE id = %s"
    refused(world, raw(move, world.issued_line.pk, draft_tax.pk))
    refused(world, raw(move, world.draft_line.pk, world.issued_tax.pk))


def test_a_line_waits_for_a_concurrent_issue_and_is_then_refused() -> None:
    """A line written while another transaction issues the invoice waits for it, then fails.

    The guard locks the parent invoice row, so the insert cannot read the draft status that the
    issuing transaction is about to replace. The rows are committed first so the second
    connection can see them.
    """
    tenant = Tenant.objects.create(name="Race")
    with tenant_context(tenant.pk):
        draft = Invoice.objects.create(
            customer=Customer.objects.create(name="Bolt"), currency="USD"
        )
    errors: list[BaseException] = []

    def add_line() -> None:
        """Insert a line into the draft from a second connection and record the outcome."""
        try:
            with tenant_context(tenant.pk):
                InvoiceLine.objects.create(
                    invoice=draft, position=1, description="Late", quantity=1, unit_price=1
                )
        except BaseException as error:  # noqa: BLE001 - the outcome is asserted below
            errors.append(error)
        finally:
            connections.close_all()

    with tenant_context(tenant.pk):
        Invoice.objects.filter(pk=draft.pk).update(status=Invoice.Status.ISSUED)
        writer = threading.Thread(target=add_line)
        writer.start()
        time.sleep(0.5)
    writer.join(timeout=10)
    assert not writer.is_alive()
    assert len(errors) == 1
    assert "issued invoices are immutable" in str(errors[0])
    with tenant_context(tenant.pk):
        assert not InvoiceLine.objects.filter(invoice=draft).exists()


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


def test_the_pdf_can_be_set_once_and_never_cleared_or_replaced(world: World) -> None:
    """An issued invoice accepts its PDF once; clearing or replacing it is refused.

    Args:
        world: Committed tenant with issued and draft invoices.
    """
    pk = world.issued.pk
    with tenant_context(world.tenant.pk):
        Invoice.objects.filter(pk=pk).update(pdf="a.pdf")
        assert Invoice.objects.get(pk=pk).pdf.name == "a.pdf"
    refused(world, lambda: Invoice.objects.filter(pk=pk).update(pdf=""))
    refused(world, lambda: Invoice.objects.filter(pk=pk).update(pdf="b.pdf"))
    refused(world, lambda: Invoice.objects.filter(pk=pk).update(pdf="a.pdf", notes="x"))
    with tenant_context(world.tenant.pk):
        assert Invoice.objects.get(pk=pk).pdf.name == "a.pdf"


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


def issue_with_product(world: World) -> tuple[Product, InvoiceLine]:
    """Issue an invoice whose line points at a new product.

    Args:
        world: World whose tenant is activated.

    Returns:
        The product and the issued line.
    """
    with tenant_context(world.tenant.pk):
        product = Product.objects.create(name="Gadget", unit_price=500)
        customer = world.issued.customer
        lines = [DraftLine("Gadget", Decimal("1"), 500, product=product)]
        issued = issue_invoice(
            world.writer, save_draft(world.writer, Invoice(customer=customer), lines)
        )
        return product, issued.lines.get()


def test_a_tax_rate_used_on_an_issued_invoice_cannot_be_deleted(world: World) -> None:
    """Deleting the rate is protected and the issued line tax keeps its link and snapshot.

    Args:
        world: World whose tenant is activated.
    """
    with tenant_context(world.tenant.pk):
        rate = world.issued_tax.tax_rate
        assert rate is not None
        with pytest.raises(ProtectedError):
            delete_tax_rate(Principal(world.tenant.pk, frozenset(Permission)), rate)
        tax = InvoiceLineTax.objects.get(pk=world.issued_tax.pk)
        assert (tax.tax_rate_id, tax.name, tax.rate_bp) == (rate.pk, "Sales", 825)


def test_a_product_used_on_an_issued_invoice_cannot_be_deleted(world: World) -> None:
    """Deleting the product is protected and the issued line keeps its link.

    Args:
        world: World whose tenant is activated.
    """
    product, line = issue_with_product(world)
    with tenant_context(world.tenant.pk):
        with pytest.raises(ProtectedError):
            delete_product(world.writer, product)
        assert InvoiceLine.objects.get(pk=line.pk).product_id == product.pk


def test_unused_products_and_tax_rates_still_delete(world: World) -> None:
    """Rows no invoice line points at are deleted as before.

    Args:
        world: World whose tenant is activated.
    """
    owner = Principal(world.tenant.pk, frozenset(Permission))
    with tenant_context(world.tenant.pk):
        product = Product.objects.create(name="Unused", unit_price=1)
        rate = TaxRate.objects.create(name="Unused", rate_bp=1)
        delete_product(owner, product)
        delete_tax_rate(owner, rate)
        assert not Product.objects.filter(name="Unused").exists()
        assert not TaxRate.objects.filter(name="Unused").exists()


def test_a_product_used_on_a_draft_is_protected_too(world: World) -> None:
    """A product used only on a draft is protected too, until the draft line goes away.

    Args:
        world: World whose tenant is activated.
    """
    with tenant_context(world.tenant.pk):
        product = Product.objects.create(name="Drafty", unit_price=1)
        lines = [DraftLine("D", Decimal("1"), 1, product=product)]
        save_draft(world.writer, Invoice(customer=world.draft.customer), lines)
        with pytest.raises(ProtectedError):
            delete_product(world.writer, product)


def test_the_admin_shows_the_protection_page_for_a_used_product(
    world: World, client: Client
) -> None:
    """The delete page of a used product explains the protection instead of failing with a 500.

    Args:
        world: World whose tenant is activated.
        client: Test client.
    """
    product, _ = issue_with_product(world)
    user = User.objects.create_superuser("root", "root@example.com", "pw")
    Membership.objects.create(user=user, tenant=world.tenant, role=Membership.Role.ADMIN)
    client.force_login(user)
    session = client.session
    session["tenant_id"] = str(world.tenant.pk)
    session.save()
    url = f"/admin/billing/product/{product.pk}/delete/"
    for response in (client.get(url), client.post(url, {"post": "yes"})):
        assert response.status_code == 200
        assert "protected" in response.content.decode()
    with tenant_context(world.tenant.pk):
        assert Product.objects.filter(pk=product.pk).exists()
