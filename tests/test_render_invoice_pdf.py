"""The Celery task that renders the PDF of an issued invoice, and its enqueueing."""

from collections.abc import Generator
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from django.test import TestCase

from billing.models import AuditEvent, Customer, Invoice
from billing.services import DraftLine, issue_invoice, save_draft
from billing.tasks import render_invoice_pdf
from tenancy.context import tenant_context
from tenancy.models import Tenant
from tenancy.permissions import Permission, Principal

pytestmark = pytest.mark.django_db


@pytest.fixture
def tenant() -> Generator[Tenant]:
    """Create a tenant with complete issuer details and make it the active one.

    Yields:
        The tenant.
    """
    tenant = Tenant.objects.create(
        name="A",
        legal_name="Acme Inc.",
        address_line1="1 Main St",
        city="Austin",
        state="TX",
        postal_code="78701",
    )
    with tenant_context(tenant.pk):
        yield tenant


def draft_for(tenant: Tenant) -> Invoice:
    """Save a draft invoice with one line.

    Args:
        tenant: Active tenant.

    Returns:
        The draft.
    """
    principal = Principal(tenant.pk, frozenset({Permission.INVOICES_WRITE}))
    customer = Customer.objects.create(name="Bolt")
    return save_draft(principal, Invoice(customer=customer), [DraftLine("Widget", Decimal(1), 500)])


def test_the_task_writes_the_pdf_at_the_tenant_path(tenant: Tenant, media_root: Path) -> None:
    """The PDF is stored under the tenant, only the pdf field changes and the system logs it.

    Args:
        tenant: Active tenant.
        media_root: Temporary media directory.
    """
    principal = Principal(tenant.pk, frozenset({Permission.INVOICES_WRITE}))
    invoice = issue_invoice(principal, draft_for(tenant))
    render_invoice_pdf(str(tenant.pk), str(invoice.pk))
    path = f"tenants/{tenant.pk}/invoices/{invoice.pk}.pdf"
    stored = Invoice.objects.get(pk=invoice.pk)
    assert stored.pdf.name == path
    assert (media_root / path).read_bytes().startswith(b"%PDF")
    assert stored.total == invoice.total
    event = AuditEvent.objects.get(action="invoice.pdf_rendered")
    assert (event.actor_kind, event.entity_id) == (AuditEvent.ActorKind.SYSTEM, invoice.pk)


def test_a_second_run_does_not_regenerate_the_document(tenant: Tenant, media_root: Path) -> None:
    """Running the task again leaves the file, the field and the audit log untouched.

    Args:
        tenant: Active tenant.
        media_root: Temporary media directory.
    """
    principal = Principal(tenant.pk, frozenset({Permission.INVOICES_WRITE}))
    invoice = issue_invoice(principal, draft_for(tenant))
    render_invoice_pdf(str(tenant.pk), str(invoice.pk))
    file = media_root / f"tenants/{tenant.pk}/invoices/{invoice.pk}.pdf"
    before = (file.stat().st_mtime_ns, file.read_bytes())
    updated_at = Invoice.objects.get(pk=invoice.pk).updated_at
    render_invoice_pdf(str(tenant.pk), str(invoice.pk))
    assert (file.stat().st_mtime_ns, file.read_bytes()) == before
    assert Invoice.objects.get(pk=invoice.pk).updated_at == updated_at
    assert AuditEvent.objects.filter(action="invoice.pdf_rendered").count() == 1
    assert [p.name for p in file.parent.iterdir()] == [file.name]


def test_a_draft_is_skipped(tenant: Tenant, media_root: Path) -> None:
    """A draft gets no file and no event.

    Args:
        tenant: Active tenant.
        media_root: Temporary media directory.
    """
    draft = draft_for(tenant)
    render_invoice_pdf(str(tenant.pk), str(draft.pk))
    assert not Invoice.objects.get(pk=draft.pk).pdf
    assert not media_root.exists()
    assert not AuditEvent.objects.filter(action="invoice.pdf_rendered").exists()


def test_an_unknown_invoice_raises(tenant: Tenant) -> None:
    """An invoice the tenant cannot see is not found.

    Args:
        tenant: Active tenant.
    """
    with pytest.raises(Invoice.DoesNotExist):
        render_invoice_pdf(str(tenant.pk), "00000000-0000-0000-0000-000000000000")


class TestEnqueue(TestCase):
    """Issuing enqueues the task after the transaction commits."""

    def test_issue_invoice_enqueues_the_task_on_commit(self) -> None:
        """The task is queued once, only after commit, with the tenant and invoice ids."""
        tenant = Tenant.objects.create(
            name="A",
            legal_name="Acme Inc.",
            address_line1="1 Main St",
            city="Austin",
            state="TX",
            postal_code="78701",
        )
        principal = Principal(tenant.pk, frozenset({Permission.INVOICES_WRITE}))
        delay = MagicMock()
        with tenant_context(tenant.pk):
            draft = draft_for(tenant)
            with (
                pytest.MonkeyPatch.context() as patch,
                self.captureOnCommitCallbacks(execute=True) as callbacks,
            ):
                patch.setattr(render_invoice_pdf, "delay", delay)
                invoice = issue_invoice(principal, draft)
                delay.assert_not_called()
        assert len(callbacks) == 1
        delay.assert_called_once_with(str(tenant.pk), str(invoice.pk))
