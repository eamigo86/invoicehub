"""The GraphQL action mutations: issue, send, credit note, and accept or reject a match."""

from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from billing.models import AuditEvent, Customer, Invoice, InvoiceEmail
from tenancy.context import tenant_context
from tenancy.models import ApiToken, Tenant
from tests.test_api_graphql import make_world as base_world
from tests.test_api_mutations import (
    ERRORS,
    READ_WRITE,
    SAVE_DRAFT,
    catalog,
    draft_input,
    make_draft,
    run,
)

pytestmark = pytest.mark.django_db(transaction=True)


def make_world(name: str, scopes: list[str]) -> tuple[Tenant, str]:
    """Create a tenant with complete issuer details, so its drafts can be issued.

    Args:
        name: Tenant name.
        scopes: Scopes granted to the token.

    Returns:
        The tenant and the plaintext token.
    """
    tenant, token = base_world(name, scopes)
    Tenant.objects.filter(pk=tenant.pk).update(
        legal_name=f"{name} Inc.",
        address_line1="1 Main St",
        city="Austin",
        state="TX",
        postal_code="78701",
    )
    return tenant, token


ISSUE = f"""
mutation Issue($id: ID!) {{
  issueInvoice(id: $id) {{ invoice {{ id status kind reference total }} {ERRORS} }}
}}
"""


def draft_for(tenant: Tenant, token: str) -> str:
    """Save a draft through the API, reusing the buyer of the tenant when it exists.

    Args:
        tenant: Tenant of the token.
        token: Plaintext API token with both scopes.

    Returns:
        The id of the draft.
    """
    with tenant_context(tenant.pk):
        buyer = Customer.objects.filter(name=f"{tenant.name} buyer").first()
    if buyer is None:
        return make_draft(tenant, token)
    saved = run(SAVE_DRAFT, token, input=draft_input(buyer))["saveDraft"]
    return str(saved["invoice"]["id"])


def issued_invoice(tenant: Tenant, token: str) -> str:
    """Draft and issue an invoice through the API.

    Args:
        tenant: Tenant of the token.
        token: Plaintext API token with both scopes.

    Returns:
        The id of the issued invoice.
    """
    draft_id = draft_for(tenant, token)
    data = run(ISSUE, token, id=draft_id)["issueInvoice"]
    assert data["errors"] == []
    return draft_id


def test_issue_invoice_numbers_the_draft_and_returns_it() -> None:
    """Issuing a draft gives it a reference and the ISSUED status."""
    tenant, token = make_world("A", READ_WRITE)
    draft_id = draft_for(tenant, token)
    data = run(ISSUE, token, id=draft_id)["issueInvoice"]
    assert data["errors"] == []
    assert data["invoice"]["id"] == draft_id
    assert (data["invoice"]["status"], data["invoice"]["kind"]) == ("ISSUED", "INVOICE")
    assert data["invoice"]["reference"].startswith("INV-")
    with tenant_context(tenant.pk):
        assert Invoice.objects.get(pk=draft_id).status == Invoice.Status.ISSUED


def test_issue_invoice_refuses_an_invoice_that_is_not_a_draft() -> None:
    """Issuing twice is an INVALID_TRANSITION entry and changes nothing."""
    tenant, token = make_world("A", READ_WRITE)
    issued_id = issued_invoice(tenant, token)
    with tenant_context(tenant.pk):
        before = Invoice.objects.get(pk=issued_id).reference
    data = run(ISSUE, token, id=issued_id)["issueInvoice"]
    assert data["invoice"] is None
    assert [e["code"] for e in data["errors"]] == ["INVALID_TRANSITION"]
    with tenant_context(tenant.pk):
        assert Invoice.objects.get(pk=issued_id).reference == before


def test_issue_invoice_reports_unknown_malformed_and_unreadable_ids() -> None:
    """A missing id is NOT_FOUND, a malformed one a validation error, no scope is denied."""
    tenant, token = make_world("A", READ_WRITE)
    draft_id = make_draft(tenant, token)
    missing = run(ISSUE, token, id="00000000-0000-0000-0000-000000000000")["issueInvoice"]
    assert [(e["code"], e["field"]) for e in missing["errors"]] == [("NOT_FOUND", "id")]
    bad = run(ISSUE, token, id="abc")["issueInvoice"]
    assert [(e["code"], e["field"]) for e in bad["errors"]] == [("VALIDATION_ERROR", "id")]
    tenant_r, token_r = make_world("R", ["invoices:read"])
    with tenant_context(tenant_r.pk):
        other = Invoice.objects.filter(status=Invoice.Status.ISSUED).first()
    denied = run(ISSUE, token_r, id=str(other.pk))["issueInvoice"]  # type: ignore[union-attr]
    assert [e["code"] for e in denied["errors"]] == ["PERMISSION_DENIED"]
    with tenant_context(tenant.pk):
        assert Invoice.objects.get(pk=draft_id).status == Invoice.Status.DRAFT


def test_issue_invoice_records_the_token_as_the_actor() -> None:
    """The audit event of an issue through a token names that token."""
    tenant, token = make_world("A", READ_WRITE)
    draft_id = make_draft(tenant, token)
    run(ISSUE, token, id=draft_id)
    with tenant_context(tenant.pk):
        event = AuditEvent.objects.get(entity_id=draft_id, action="invoice.issued")
        assert event.actor_kind == AuditEvent.ActorKind.TOKEN
        assert event.actor_token == ApiToken.objects.get()


SEND = f"""
mutation Send($input: SendInvoiceInput!) {{
  sendInvoice(input: $input) {{ invoice {{ id status }} {ERRORS} }}
}}
"""


def test_send_invoice_enqueues_one_email_per_idempotency_key(enqueued_email: MagicMock) -> None:
    """Repeating a key returns the invoice again and enqueues no second email.

    Args:
        enqueued_email: Mock standing in for the email task delay.
    """
    tenant, token = make_world("A", READ_WRITE)
    invoice_id = issued_invoice(tenant, token)
    request = {"id": invoice_id, "idempotencyKey": "k-1", "recipients": ["ap@buyer.test"]}
    first = run(SEND, token, input=request)["sendInvoice"]
    again = run(SEND, token, input=request)["sendInvoice"]
    assert first == again == {"invoice": {"id": invoice_id, "status": "ISSUED"}, "errors": []}
    enqueued_email.assert_called_once()
    with tenant_context(tenant.pk):
        email = InvoiceEmail.objects.get()
        assert (email.invoice_id, email.recipients) == (UUID(invoice_id), ["ap@buyer.test"])
        assert AuditEvent.objects.filter(action="invoice.send_requested").count() == 1


def test_send_invoice_without_recipients_uses_the_customer_emails(
    enqueued_email: MagicMock,
) -> None:
    """Omitting the recipients sends to the emails frozen on the issued invoice.

    Args:
        enqueued_email: Mock standing in for the email task delay.
    """
    tenant, token = make_world("A", READ_WRITE)
    buyer, _, _ = catalog(tenant)
    with tenant_context(tenant.pk):
        Customer.objects.filter(pk=buyer.pk).update(emails=["frozen@buyer.test"])
    invoice_id = issued_invoice(tenant, token)
    data = run(SEND, token, input={"id": invoice_id, "idempotencyKey": "k-1"})["sendInvoice"]
    assert data["errors"] == []
    with tenant_context(tenant.pk):
        assert InvoiceEmail.objects.get().recipients == ["frozen@buyer.test"]
    enqueued_email.assert_called_once()


def test_send_invoice_reusing_a_key_for_another_invoice_is_a_validation_error(
    enqueued_email: MagicMock,
) -> None:
    """The key belongs to the first invoice, so the second request is refused.

    Args:
        enqueued_email: Mock standing in for the email task delay.
    """
    tenant, token = make_world("A", READ_WRITE)
    first, second = issued_invoice(tenant, token), issued_invoice(tenant, token)
    recipients = ["ap@buyer.test"]
    run(SEND, token, input={"id": first, "idempotencyKey": "k", "recipients": recipients})
    data = run(SEND, token, input={"id": second, "idempotencyKey": "k", "recipients": recipients})[
        "sendInvoice"
    ]
    assert data["invoice"] is None
    assert [e["code"] for e in data["errors"]] == ["VALIDATION_ERROR"]
    enqueued_email.assert_called_once()


def test_send_invoice_reusing_a_key_with_other_recipients_changes_nothing(
    enqueued_email: MagicMock,
) -> None:
    """The service is idempotent on the key alone, so new recipients are ignored.

    Args:
        enqueued_email: Mock standing in for the email task delay.
    """
    tenant, token = make_world("A", READ_WRITE)
    invoice_id = issued_invoice(tenant, token)
    run(SEND, token, input={"id": invoice_id, "idempotencyKey": "k", "recipients": ["a@x.test"]})
    data = run(
        SEND, token, input={"id": invoice_id, "idempotencyKey": "k", "recipients": ["b@x.test"]}
    )["sendInvoice"]
    assert data["errors"] == []
    enqueued_email.assert_called_once()
    with tenant_context(tenant.pk):
        assert InvoiceEmail.objects.get().recipients == ["a@x.test"]


def test_send_invoice_reports_each_refusal_as_a_typed_error(enqueued_email: MagicMock) -> None:
    """Empty key, bad address, no recipient, draft, unknown id and a read-only token.

    Args:
        enqueued_email: Mock standing in for the email task delay.
    """
    tenant, token = make_world("A", READ_WRITE)
    invoice_id = issued_invoice(tenant, token)
    draft_id = draft_for(tenant, token)

    def send(token: str = token, **request: Any) -> list[tuple[str, str | None]]:
        data = run(SEND, token, input={"id": invoice_id, "idempotencyKey": "k", **request})
        return [(e["code"], e["field"]) for e in data["sendInvoice"]["errors"]]

    assert send(idempotencyKey="") == [("VALIDATION_ERROR", "idempotencyKey")]
    assert send(recipients=["nope"]) == [("VALIDATION_ERROR", "recipients")]
    assert send() == [("VALIDATION_ERROR", None)]
    assert send(id=draft_id) == [("INVALID_TRANSITION", None)]
    assert send(id="00000000-0000-0000-0000-000000000000") == [("NOT_FOUND", "id")]
    tenant_r, token_r = make_world("R", ["invoices:read"])
    with tenant_context(tenant_r.pk):
        readable = str(Invoice.objects.first().pk)  # type: ignore[union-attr]
    assert send(token_r, id=readable) == [("PERMISSION_DENIED", None)]
    enqueued_email.assert_not_called()
    with tenant_context(tenant.pk):
        assert not InvoiceEmail.objects.exists()


CREDIT = f"""
mutation Credit($input: CreditNoteInput!) {{
  draftCreditNote(input: $input) {{
    creditNote {{ id kind status currency subtotal taxTotal total lines {{ totalCount }} }}
    {ERRORS}
  }}
}}
"""


def test_draft_credit_note_without_lines_credits_the_whole_invoice() -> None:
    """A full credit copies the invoice, leaves it alone, and is issued like any draft."""
    tenant, token = make_world("A", READ_WRITE)
    invoice_id = issued_invoice(tenant, token)
    with tenant_context(tenant.pk):
        original = Invoice.objects.get(pk=invoice_id)
    data = run(CREDIT, token, input={"invoiceId": invoice_id})["draftCreditNote"]
    assert data["errors"] == []
    note = data["creditNote"]
    assert (note["kind"], note["status"], note["currency"]) == ("CREDIT_NOTE", "DRAFT", "USD")
    assert note["total"] == str(original.total)
    assert note["lines"]["totalCount"] == 2
    with tenant_context(tenant.pk):
        stored = Invoice.objects.get(pk=note["id"])
        assert stored.original_id == original.pk
        assert Invoice.objects.get(pk=invoice_id).status == Invoice.Status.ISSUED
    issued = run(ISSUE, token, id=note["id"])["issueInvoice"]
    assert issued["errors"] == []
    assert (issued["invoice"]["kind"], issued["invoice"]["status"]) == ("CREDIT_NOTE", "ISSUED")


def test_draft_credit_note_with_lines_is_a_partial_credit() -> None:
    """The lines given are the lines of the note, in the same input as a draft line."""
    tenant, token = make_world("A", READ_WRITE)
    invoice_id = issued_invoice(tenant, token)
    lines = [{"description": "Goodwill", "quantity": "1", "unitPrice": "500"}]
    data = run(CREDIT, token, input={"invoiceId": invoice_id, "lines": lines})["draftCreditNote"]
    assert data["errors"] == []
    assert (data["creditNote"]["total"], data["creditNote"]["lines"]["totalCount"]) == ("500", 1)


def test_draft_credit_note_reports_each_refusal_as_a_typed_error() -> None:
    """A draft original, bad line values, unknown ids and a read-only token."""
    tenant, token = make_world("A", READ_WRITE)
    invoice_id = issued_invoice(tenant, token)
    draft_id = draft_for(tenant, token)

    def credit(token: str = token, **request: Any) -> list[tuple[str, str | None]]:
        data = run(CREDIT, token, input={"invoiceId": invoice_id, **request})
        return [(e["code"], e["field"]) for e in data["draftCreditNote"]["errors"]]

    line = {"description": "x", "quantity": "1", "unitPrice": "1"}
    assert credit(invoiceId=draft_id) == [("INVALID_TRANSITION", None)]
    assert credit(invoiceId="00000000-0000-0000-0000-000000000000") == [("NOT_FOUND", "invoiceId")]
    assert credit(invoiceId="abc") == [("VALIDATION_ERROR", "invoiceId")]
    assert credit(lines=[{**line, "quantity": "1.2345"}]) == [
        ("VALIDATION_ERROR", "lines[0].quantity")
    ]
    assert credit(lines=[{**line, "productId": "999999"}]) == [("NOT_FOUND", "lines[0].productId")]
    tenant_r, token_r = make_world("R", ["invoices:read"])
    with tenant_context(tenant_r.pk):
        readable = str(Invoice.objects.first().pk)  # type: ignore[union-attr]
    assert credit(token_r, invoiceId=readable) == [("PERMISSION_DENIED", None)]
    with tenant_context(tenant.pk):
        assert not Invoice.objects.filter(kind=Invoice.Kind.CREDIT_NOTE).exists()
