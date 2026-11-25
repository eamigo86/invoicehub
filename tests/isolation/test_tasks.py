"""Celery tasks never act across tenants, whatever tenant id they are handed."""

from decimal import Decimal
from pathlib import Path

import pytest
from django.core import mail

from billing.models import AuditEvent, Customer, Invoice, InvoiceEmail
from billing.services import DraftLine, issue_invoice, save_draft, send_invoice
from billing.tasks import render_invoice_pdf, send_invoice_email
from tenancy.context import tenant_context
from tenancy.models import Tenant
from tenancy.permissions import Permission, Principal

pytestmark = [pytest.mark.isolation, pytest.mark.django_db]


def test_b_cannot_render_the_pdf_of_an_invoice_of_a(media_root: Path) -> None:
    """Under B, A's invoice is not found: no file is written and A's invoice is unchanged.

    Args:
        media_root: Temporary media directory.
    """
    a = Tenant.objects.create(
        name="A",
        legal_name="A Inc.",
        address_line1="1 Main St",
        city="Austin",
        state="TX",
        postal_code="78701",
    )
    b = Tenant.objects.create(name="B")
    principal = Principal(a.pk, frozenset(Permission))
    with tenant_context(a.pk):
        customer = Customer.objects.create(name="Client")
        draft = save_draft(principal, Invoice(customer=customer), [DraftLine("L", Decimal(1), 100)])
        invoice = issue_invoice(principal, draft)
        before = Invoice.objects.get(pk=invoice.pk)
    with pytest.raises(Invoice.DoesNotExist):
        render_invoice_pdf(str(b.pk), str(invoice.pk))
    assert not media_root.exists()
    with tenant_context(a.pk):
        after = Invoice.objects.get(pk=invoice.pk)
        assert (after.pdf.name, after.updated_at) == ("", before.updated_at)
        assert not AuditEvent.objects.filter(action="invoice.pdf_rendered").exists()


def test_b_cannot_send_the_email_request_of_a() -> None:
    """Under B, A's request is not found: nothing is mailed and A's rows are unchanged."""
    a = Tenant.objects.create(
        name="A",
        legal_name="A Inc.",
        address_line1="1 Main St",
        city="Austin",
        state="TX",
        postal_code="78701",
    )
    b = Tenant.objects.create(name="B")
    principal = Principal(a.pk, frozenset(Permission))
    with tenant_context(a.pk):
        customer = Customer.objects.create(name="Client", emails=["ap@a.example"])
        draft = save_draft(principal, Invoice(customer=customer), [DraftLine("L", Decimal(1), 100)])
        invoice = issue_invoice(principal, draft)
        request = send_invoice(principal, invoice, "k")
    with pytest.raises(InvoiceEmail.DoesNotExist):
        send_invoice_email(str(b.pk), str(request.pk))
    assert mail.outbox == []
    with tenant_context(a.pk):
        stored = Invoice.objects.get(pk=invoice.pk)
        assert (stored.status, stored.pdf.name) == (Invoice.Status.ISSUED, "")
        assert InvoiceEmail.objects.get().sent_at is None
        assert not AuditEvent.objects.filter(action="invoice.sent").exists()
