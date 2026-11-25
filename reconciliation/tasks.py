"""Celery tasks of the reconciliation app."""

import time

from django.utils import timezone

from billing.models import Invoice
from config.celery import app
from reconciliation import engine
from reconciliation.models import (
    BankAccount,
    BankStatementImport,
    BankTransaction,
    MatchProposal,
    MatchProposalInvoice,
    MatchProposalTransaction,
    ReconciliationRun,
)
from reconciliation.parsing import StatementError, parse_statement
from tenancy.context import tenant_context


@app.task(name="reconciliation.tasks.import_statement", shared=False)  # type: ignore[untyped-decorator]
def import_statement(tenant_id: str, import_id: str) -> None:
    """Parse an uploaded bank statement into transactions once.

    The import row is locked and one that is not pending is left alone, so a second delivery
    never imports again. The account row is locked too, so overlapping imports of one account
    run one after the other and the second sees the first one's transactions. A file that
    cannot be parsed marks the import failed with the error message and writes nothing else.
    Otherwise the rows whose dedupe hash is already stored are counted as duplicates, the new
    ones are created, and the import is marked completed with its counters. An import the
    tenant cannot see raises BankStatementImport.DoesNotExist and nothing is written.

    Args:
        tenant_id: Tenant that owns the import.
        import_id: Import to run.
    """
    # ponytail: delivery is at least once. If the commit fails after the work, the import stays
    # pending and a retry redoes it, which is safe because transactions are deduplicated.
    with tenant_context(tenant_id):
        row = BankStatementImport.objects.select_for_update().get(pk=import_id)
        if row.status != BankStatementImport.Status.PENDING:
            return
        BankAccount.objects.select_for_update().get(pk=row.bank_account_id)
        with row.file.open("rb") as file:
            content = file.read()
        row.completed_at = timezone.now()
        try:
            parsed = parse_statement(content, row.mapping, str(row.bank_account_id))
        except StatementError as exc:
            row.status = BankStatementImport.Status.FAILED
            row.error = str(exc)
            row.save(update_fields=["status", "error", "completed_at"])
            return
        known = set(
            BankTransaction.objects.filter(
                dedupe_hash__in=[p.dedupe_hash for p in parsed]
            ).values_list("dedupe_hash", flat=True)
        )
        created = BankTransaction.objects.bulk_create(
            [
                BankTransaction(
                    tenant_id=row.tenant_id,
                    bank_account_id=row.bank_account_id,
                    statement_import=row,
                    value_date=p.value_date,
                    amount=p.amount,
                    description=p.description,
                    payer=p.payer,
                    dedupe_hash=p.dedupe_hash,
                )
                for p in parsed
                if p.dedupe_hash not in known
            ]
        )
        row.status = BankStatementImport.Status.COMPLETED
        row.rows_read = len(parsed)
        row.duplicates = len(parsed) - len(created)
        row.imported = len(created)
        row.save(update_fields=["status", "rows_read", "duplicates", "imported", "completed_at"])


# A transaction in any of these states is spoken for; rejected and undone proposals free it.
HELD_STATES = (
    MatchProposal.State.PENDING,
    MatchProposal.State.APPLIED,
    MatchProposal.State.REASSIGNED,
)


@app.task(name="reconciliation.tasks.reconcile_run", shared=False)  # type: ignore[untyped-decorator]
def reconcile_run(tenant_id: str, run_id: str) -> None:
    """Propose matches between free bank credits and open invoices once.

    The run row is locked and one that is not pending is left alone, so a second delivery never
    proposes again. Free transactions are credits that no pending, applied or reassigned
    proposal holds. Open invoices are issued invoices with a payable status and a balance due
    above zero that no pending proposal holds. The engine call is timed. If the engine raises
    ValueError or TypeError (a bug of the caller) or OverflowError (bad data), the run is marked
    failed with the error and no proposal is written. Otherwise each match becomes a pending
    proposal linked to its transactions and invoices, and the run is marked completed with its
    counters. A run the tenant cannot see raises ReconciliationRun.DoesNotExist and nothing is
    written.

    Args:
        tenant_id: Tenant that owns the run.
        run_id: Run to execute.
    """
    with tenant_context(tenant_id):
        run = ReconciliationRun.objects.select_for_update().get(pk=run_id)
        if run.status != ReconciliationRun.Status.PENDING:
            return
        transactions = list(
            BankTransaction.objects.filter(amount__gt=0)
            .exclude(proposals__state__in=HELD_STATES)
            .select_related("bank_account")
        )
        held_invoices = MatchProposalInvoice.objects.filter(
            proposal__state=MatchProposal.State.PENDING
        ).values("invoice_id")
        # ponytail: one balance query per invoice. Upgrade path: annotate the balance in the
        # queryset with an aggregate over allocations and credit notes.
        invoices = [
            (invoice, balance)
            for invoice in Invoice.objects.filter(
                kind=Invoice.Kind.INVOICE, status__in=Invoice.PAYABLE_STATUSES
            ).exclude(pk__in=held_invoices)
            if (balance := invoice.balance_due()) > 0
        ]
        started = time.monotonic()
        try:
            result = engine.match(invoices, transactions, run.config, str(run.tenant_id))
        except (ValueError, TypeError, OverflowError) as exc:
            run.status = ReconciliationRun.Status.FAILED
            run.error = f"{type(exc).__name__}: {exc}"
            run.completed_at = timezone.now()
            run.save(update_fields=["status", "error", "completed_at"])
            return
        run.duration_ms = int((time.monotonic() - started) * 1000)
        matches = result.matches
        by_tx = {str(tx.pk): tx for tx in transactions}
        by_invoice = {str(invoice.pk): invoice for invoice, _ in invoices}
        for match in matches:
            proposal = MatchProposal.objects.create(
                run=run,
                kind=match.kind.value,
                engine_status=match.status.value,
                score=match.score,
                breakdown=dict(match.breakdown),
                residual=match.residual_minor,
            )
            MatchProposalTransaction.objects.bulk_create(
                MatchProposalTransaction(
                    tenant_id=run.tenant_id, proposal=proposal, transaction=by_tx[i]
                )
                for i in match.payment_ids
            )
            MatchProposalInvoice.objects.bulk_create(
                MatchProposalInvoice(
                    tenant_id=run.tenant_id, proposal=proposal, invoice=by_invoice[i]
                )
                for i in match.invoice_ids
            )
        statuses = [match.status.value for match in matches]
        run.status = ReconciliationRun.Status.COMPLETED
        run.transactions_considered = len(transactions)
        run.invoices_considered = len(invoices)
        run.auto_count = statuses.count("auto")
        run.suggested_count = statuses.count("suggested")
        run.ambiguous_count = statuses.count("ambiguous")
        run.unmatched_transactions = len(result.unmatched_payments)
        run.unmatched_invoices = len(result.unmatched_invoices)
        run.completed_at = timezone.now()
        run.save(
            update_fields=[
                "status",
                "duration_ms",
                "transactions_considered",
                "invoices_considered",
                "auto_count",
                "suggested_count",
                "ambiguous_count",
                "unmatched_transactions",
                "unmatched_invoices",
                "completed_at",
            ]
        )
