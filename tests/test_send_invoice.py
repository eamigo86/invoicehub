"""Emailing an invoice: the send service, its idempotency and the delivery task."""

from collections.abc import Generator
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from billing.models import AuditEvent, Customer, Invoice, InvoiceEmail
from billing.services import (
    DraftLine,
    InvalidTransition,
    issue_invoice,
    save_draft,
    send_invoice,
)
from billing.tasks import send_invoice_email
from tenancy.context import tenant_context
from tenancy.models import Tenant
from tenancy.permissions import Permission, Principal

pytestmark = pytest.mark.django_db

EMAILS = ["ap@bolt.example", "boss@bolt.example"]


def principal_of(tenant: Tenant) -> Principal:
    """Build a principal who may write invoices.

    Args:
        tenant: Tenant the principal acts in.

    Returns:
        The principal.
    """
    return Principal(tenant.pk, frozenset({Permission.INVOICES_WRITE}))


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


def draft_for(tenant: Tenant, emails: list[str] | None = None) -> Invoice:
    """Save a draft invoice for a customer with the given emails.

    Args:
        tenant: Active tenant.
        emails: Customer emails, the standard two when omitted.

    Returns:
        The draft.
    """
    customer = Customer.objects.create(name="Bolt", emails=EMAILS if emails is None else emails)
    return save_draft(
        principal_of(tenant), Invoice(customer=customer), [DraftLine("Widget", Decimal(1), 500)]
    )


def issued_for(tenant: Tenant, emails: list[str] | None = None) -> Invoice:
    """Issue a new invoice.

    Args:
        tenant: Active tenant.
        emails: Customer emails, the standard two when omitted.

    Returns:
        The issued invoice.
    """
    return issue_invoice(principal_of(tenant), draft_for(tenant, emails))


def test_the_request_defaults_to_the_customer_emails(tenant: Tenant) -> None:
    """The request is stored unsent with the snapshot emails and an audit event of the actor.

    Args:
        tenant: Active tenant.
    """
    invoice = issued_for(tenant)
    user = get_user_model().objects.create_user("ann", "ann@example.com", "pw")
    actor = Principal(tenant.pk, frozenset({Permission.INVOICES_WRITE}), user_id=user.pk)
    request = send_invoice(actor, invoice, "key-1")
    stored = InvoiceEmail.objects.get()
    assert stored.pk == request.pk
    assert (stored.invoice_id, stored.idempotency_key) == (invoice.pk, "key-1")
    assert (stored.recipients, stored.sent_at) == (EMAILS, None)
    event = AuditEvent.objects.get(action="invoice.send_requested")
    assert (event.actor_kind, event.entity_id) == (AuditEvent.ActorKind.USER, invoice.pk)
    assert event.after is not None
    assert event.after["recipients"] == EMAILS


def test_explicit_recipients_replace_the_customer_emails(tenant: Tenant) -> None:
    """Given recipients are stored instead of the snapshot emails.

    Args:
        tenant: Active tenant.
    """
    request = send_invoice(principal_of(tenant), issued_for(tenant), "k", ["x@y.example"])
    assert request.recipients == ["x@y.example"]


def test_the_same_key_returns_the_first_request_and_does_nothing_more(tenant: Tenant) -> None:
    """A replay creates no row and adds no audit event.

    Args:
        tenant: Active tenant.
    """
    invoice = issued_for(tenant)
    first = send_invoice(principal_of(tenant), invoice, "same")
    second = send_invoice(principal_of(tenant), invoice, "same", ["other@y.example"])
    assert second.pk == first.pk
    assert InvoiceEmail.objects.count() == 1
    assert AuditEvent.objects.filter(action="invoice.send_requested").count() == 1


def test_a_key_of_another_invoice_is_refused(tenant: Tenant) -> None:
    """A key already used for a different invoice is not silently reused.

    Args:
        tenant: Active tenant.
    """
    send_invoice(principal_of(tenant), issued_for(tenant), "k")
    with pytest.raises(ValidationError):
        send_invoice(principal_of(tenant), issued_for(tenant), "k")
    assert InvoiceEmail.objects.count() == 1


@pytest.mark.parametrize("status", [Invoice.Status.SENT, Invoice.Status.PAID])
def test_later_statuses_can_be_sent(tenant: Tenant, status: str) -> None:
    """A sent or paid invoice can be sent again.

    Args:
        tenant: Active tenant.
        status: Status of the invoice.
    """
    invoice = issued_for(tenant)
    Invoice.objects.filter(pk=invoice.pk).update(status=status)
    assert send_invoice(principal_of(tenant), invoice, "k").pk


def test_a_draft_is_refused(tenant: Tenant) -> None:
    """A draft cannot be emailed and nothing is stored.

    Args:
        tenant: Active tenant.
    """
    with pytest.raises(InvalidTransition):
        send_invoice(principal_of(tenant), draft_for(tenant), "k")
    assert not InvoiceEmail.objects.exists()


def test_a_voided_invoice_is_refused(tenant: Tenant) -> None:
    """A voided invoice cannot be emailed.

    Args:
        tenant: Active tenant.
    """
    invoice = issued_for(tenant)
    Invoice.objects.filter(pk=invoice.pk).update(status=Invoice.Status.VOIDED)
    with pytest.raises(InvalidTransition):
        send_invoice(principal_of(tenant), invoice, "k")
    assert not InvoiceEmail.objects.exists()


def test_no_recipients_is_refused(tenant: Tenant) -> None:
    """A customer without emails and no given recipients cannot be emailed.

    Args:
        tenant: Active tenant.
    """
    invoice = issued_for(tenant, emails=[])
    with pytest.raises(ValidationError):
        send_invoice(principal_of(tenant), invoice, "k")
    with pytest.raises(ValidationError):
        send_invoice(principal_of(tenant), issued_for(tenant), "k2", [])
    assert not InvoiceEmail.objects.exists()


def test_a_viewer_is_refused(tenant: Tenant) -> None:
    """A principal without invoices:write cannot request a send.

    Args:
        tenant: Active tenant.
    """
    invoice = issued_for(tenant)
    viewer = Principal(tenant.pk, frozenset({Permission.INVOICES_READ}))
    with pytest.raises(PermissionDenied):
        send_invoice(viewer, invoice, "k")
    assert not InvoiceEmail.objects.exists()


def test_the_task_sends_one_email_with_the_pdf(tenant: Tenant, media_root: Path) -> None:
    """One mail goes to the snapshot emails with the PDF attached, and the invoice becomes sent.

    Args:
        tenant: Active tenant.
        media_root: Temporary media directory.
    """
    invoice = issued_for(tenant)
    request = send_invoice(principal_of(tenant), invoice, "k")
    send_invoice_email(str(tenant.pk), str(request.pk))
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == EMAILS
    assert "Acme Inc." in message.subject and invoice.reference in message.subject
    assert "5.00" in message.body and "Acme Inc." in message.body
    (name, content, mimetype) = message.attachments[0]
    assert name == f"{invoice.reference}.pdf"
    assert isinstance(content, bytes) and content.startswith(b"%PDF")
    assert mimetype == "application/pdf"
    stored = Invoice.objects.get(pk=invoice.pk)
    assert stored.status == Invoice.Status.SENT
    assert InvoiceEmail.objects.get().sent_at is not None
    event = AuditEvent.objects.get(action="invoice.sent")
    assert (event.actor_kind, event.entity_id) == (AuditEvent.ActorKind.SYSTEM, invoice.pk)
    assert event.after is not None
    assert event.after["recipients"] == EMAILS


def test_the_task_sends_to_the_given_recipients(tenant: Tenant) -> None:
    """Given recipients, not the customer emails, receive the mail.

    Args:
        tenant: Active tenant.
    """
    request = send_invoice(principal_of(tenant), issued_for(tenant), "k", ["x@y.example"])
    send_invoice_email(str(tenant.pk), str(request.pk))
    assert [m.to for m in mail.outbox] == [["x@y.example"]]


def test_a_second_run_sends_nothing(tenant: Tenant) -> None:
    """Running the task again neither mails nor audits nor moves sent_at.

    Args:
        tenant: Active tenant.
    """
    request = send_invoice(principal_of(tenant), issued_for(tenant), "k")
    send_invoice_email(str(tenant.pk), str(request.pk))
    sent_at = InvoiceEmail.objects.get().sent_at
    send_invoice_email(str(tenant.pk), str(request.pk))
    assert len(mail.outbox) == 1
    assert InvoiceEmail.objects.get().sent_at == sent_at
    assert AuditEvent.objects.filter(action="invoice.sent").count() == 1


def test_a_paid_invoice_stays_paid(tenant: Tenant) -> None:
    """Only an issued invoice moves to sent; a paid one keeps its status.

    Args:
        tenant: Active tenant.
    """
    invoice = issued_for(tenant)
    Invoice.objects.filter(pk=invoice.pk).update(status=Invoice.Status.PAID)
    request = send_invoice(principal_of(tenant), invoice, "k")
    send_invoice_email(str(tenant.pk), str(request.pk))
    assert len(mail.outbox) == 1
    assert Invoice.objects.get(pk=invoice.pk).status == Invoice.Status.PAID


def test_the_task_renders_a_missing_pdf(tenant: Tenant) -> None:
    """The PDF is rendered on the spot when the render task has not run yet.

    Args:
        tenant: Active tenant.
    """
    invoice = issued_for(tenant)
    assert not Invoice.objects.get(pk=invoice.pk).pdf
    request = send_invoice(principal_of(tenant), invoice, "k")
    send_invoice_email(str(tenant.pk), str(request.pk))
    assert Invoice.objects.get(pk=invoice.pk).pdf
    assert AuditEvent.objects.filter(action="invoice.pdf_rendered").count() == 1


class TestEnqueue(TestCase):
    """Requesting a send enqueues the task after the transaction commits."""

    def test_send_invoice_enqueues_the_task_on_commit(self) -> None:
        """The task is queued once, only after commit, and a replay queues nothing."""
        tenant = Tenant.objects.create(
            name="A",
            legal_name="Acme Inc.",
            address_line1="1 Main St",
            city="Austin",
            state="TX",
            postal_code="78701",
        )
        delay = MagicMock()
        with tenant_context(tenant.pk):
            invoice = issued_for(tenant)
            with (
                pytest.MonkeyPatch.context() as patch,
                self.captureOnCommitCallbacks(execute=True) as callbacks,
            ):
                patch.setattr(send_invoice_email, "delay", delay)
                request = send_invoice(principal_of(tenant), invoice, "k")
                delay.assert_not_called()
            with (
                pytest.MonkeyPatch.context() as patch,
                self.captureOnCommitCallbacks(execute=True) as replay,
            ):
                patch.setattr(send_invoice_email, "delay", delay)
                send_invoice(principal_of(tenant), invoice, "k")
        assert len(callbacks) == 1 and not replay
        delay.assert_called_once_with(str(tenant.pk), str(request.pk))
