"""Celery tasks never act across tenants, whatever tenant id they are handed."""

from decimal import Decimal
from pathlib import Path

import pytest
from django.core import mail

from billing.models import AuditEvent, Customer, Invoice, InvoiceEmail, Payment
from billing.services import DraftLine, issue_invoice, save_draft, send_invoice
from billing.tasks import render_invoice_pdf, send_invoice_email
from reconciliation.models import (
    BankAccount,
    BankStatementImport,
    BankTransaction,
    MatchProposal,
    ReconciliationRun,
)
from reconciliation.services import create_import, run_reconciliation
from reconciliation.tasks import import_statement, reconcile_run
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


def test_b_cannot_run_the_import_of_a() -> None:
    """Under B, A's import is not found: it stays pending and no transaction is created."""
    a = Tenant.objects.create(name="A")
    b = Tenant.objects.create(name="B")
    principal = Principal(a.pk, frozenset(Permission))
    with tenant_context(a.pk):
        account = BankAccount.objects.create(
            name="Chase",
            account_identifier="1234",
            date_column="Date",
            amount_column="Amount",
            description_column="Memo",
        )
        row = create_import(principal, account, b"Date,Amount,Memo\n01/31/2026,10.00,Rent\n")
    with pytest.raises(BankStatementImport.DoesNotExist):
        import_statement(str(b.pk), str(row.pk))
    with tenant_context(a.pk):
        stored = BankStatementImport.objects.get(pk=row.pk)
        assert (stored.status, stored.completed_at) == (BankStatementImport.Status.PENDING, None)
        assert not BankTransaction.objects.exists()
    with tenant_context(b.pk):
        assert not BankTransaction.objects.exists()


def test_b_cannot_run_the_reconciliation_of_a() -> None:
    """Under B, A's run is not found: it stays pending and no proposal is written."""
    a = Tenant.objects.create(name="A")
    b = Tenant.objects.create(name="B")
    with tenant_context(a.pk):
        run = run_reconciliation(Principal(a.pk, frozenset(Permission)))
    with pytest.raises(ReconciliationRun.DoesNotExist):
        reconcile_run(str(b.pk), str(run.pk))
    with tenant_context(a.pk):
        stored = ReconciliationRun.objects.get(pk=run.pk)
        assert (stored.status, stored.completed_at) == (ReconciliationRun.Status.PENDING, None)
        assert not MatchProposal.objects.exists()
    with tenant_context(b.pk):
        assert not ReconciliationRun.objects.exists()


def test_a_run_of_a_never_considers_the_rows_of_b() -> None:
    """A's run proposes only A's rows, even when B has a payment and invoice that fit."""
    a = Tenant.objects.create(
        name="A",
        legal_name="A Inc.",
        address_line1="1 Main St",
        city="Austin",
        state="TX",
        postal_code="78701",
    )
    b = Tenant.objects.create(
        name="B",
        legal_name="B Inc.",
        address_line1="2 Main St",
        city="Austin",
        state="TX",
        postal_code="78701",
    )
    for tenant in (a, b):
        principal = Principal(tenant.pk, frozenset(Permission))
        with tenant_context(tenant.pk):
            customer = Customer.objects.create(name="Client")
            draft = save_draft(
                principal, Invoice(customer=customer), [DraftLine("L", Decimal(1), 100)]
            )
            invoice = issue_invoice(principal, draft)
            row = create_import(
                principal,
                BankAccount.objects.create(
                    name="Chase",
                    account_identifier="1234",
                    date_column="Date",
                    amount_column="Amount",
                    description_column="Memo",
                ),
                f"Date,Amount,Memo\n{invoice.issue_date:%m/%d/%Y},1.00,{invoice.reference}\n".encode(),
            )
            import_statement(str(tenant.pk), str(row.pk))
    with tenant_context(a.pk):
        run = run_reconciliation(Principal(a.pk, frozenset(Permission)))
    reconcile_run(str(a.pk), str(run.pk))
    with tenant_context(a.pk):
        stored = ReconciliationRun.objects.get(pk=run.pk)
        assert (stored.transactions_considered, stored.invoices_considered) == (1, 1)
        proposal = MatchProposal.objects.get()
        assert proposal.transactions.get().tenant_id == a.pk
        assert proposal.invoices.get().tenant_id == a.pk
    with tenant_context(b.pk):
        assert not MatchProposal.objects.exists()


def test_a_run_of_b_applies_only_the_rows_of_b() -> None:
    """B's auto match is applied; A's transaction gets no payment and A's invoice is untouched."""
    tenants = [
        Tenant.objects.create(
            name=name,
            legal_name=f"{name} Inc.",
            address_line1="1 Main St",
            city="Austin",
            state="TX",
            postal_code="78701",
        )
        for name in ("A", "B")
    ]
    a, b = tenants
    for tenant in tenants:
        principal = Principal(tenant.pk, frozenset(Permission))
        with tenant_context(tenant.pk):
            customer = Customer.objects.create(name="Client")
            draft = save_draft(
                principal, Invoice(customer=customer), [DraftLine("L", Decimal(1), 100)]
            )
            invoice = issue_invoice(principal, draft)
            row = create_import(
                principal,
                BankAccount.objects.create(
                    name="Chase",
                    account_identifier="1234",
                    date_column="Date",
                    amount_column="Amount",
                    description_column="Memo",
                    payer_column="Payer",
                ),
                (
                    "Date,Amount,Memo,Payer\n"
                    f"{invoice.issue_date:%m/%d/%Y},1.00,{invoice.reference},Client\n"
                ).encode(),
            )
            import_statement(str(tenant.pk), str(row.pk))
    with tenant_context(b.pk):
        run = run_reconciliation(Principal(b.pk, frozenset(Permission)))
    reconcile_run(str(b.pk), str(run.pk))
    with tenant_context(b.pk):
        stored = ReconciliationRun.objects.get(pk=run.pk)
        assert (stored.transactions_considered, stored.invoices_considered) == (1, 1)
        assert stored.applied_count == 1
        assert BankTransaction.objects.get().payment is not None
        assert Invoice.objects.get().status == Invoice.Status.PAID
        assert MatchProposal.objects.get().state == MatchProposal.State.APPLIED
    with tenant_context(a.pk):
        assert BankTransaction.objects.get().payment is None
        assert Invoice.objects.get().status == Invoice.Status.ISSUED
        assert not MatchProposal.objects.exists()
        assert not Payment.objects.exists()
