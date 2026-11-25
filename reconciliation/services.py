"""Reconciliation services: the write path for bank statement imports.

Views, the admin and later the API call these functions. Cross-tenant references are refused
the same way as in billing: Row-Level Security hides the rows of other tenants, so an account
of another tenant is not found.
"""

import hashlib

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from billing.models import Invoice, Payment
from billing.services import InvalidTransition, record_event, record_payment
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
from tenancy.context import require_current_tenant_id
from tenancy.permissions import Permission, Principal, require_permission

MAX_STATEMENT_BYTES = 10 * 1024 * 1024
"""Largest bank statement, in bytes, that an upload may have."""


@transaction.atomic
def create_import(
    principal: Principal | None, bank_account: BankAccount, content: bytes
) -> BankStatementImport:
    """Store an uploaded bank statement once per account and queue its import.

    The account row is locked, so uploads for one account are serialized. The SHA-256 of the
    content is the upload idempotency key: a file already stored for the account returns its
    import without storing, enqueueing or changing anything again. The exception is a failed
    import: it is reset to pending with the current account mapping, its error, completion time
    and counters cleared, so a file that failed on a wrong mapping can be uploaded again. A new
    file is stored under the tenant with a snapshot of the account mapping. Either way the
    import is validated and saved, and once the transaction commits the task import_statement is
    enqueued to parse it. An account the tenant cannot see raises BankAccount.DoesNotExist and
    nothing is written. Content over MAX_STATEMENT_BYTES is refused before anything is read
    from the database.

    Args:
        principal: Acting principal, who needs reconciliation:write or PermissionDenied is
            raised.
        bank_account: Account the statement belongs to.
        content: Raw bytes of the uploaded CSV.

    Returns:
        The stored import, which is the earlier one when the same file was already uploaded.

    Raises:
        ValidationError: When the content is larger than MAX_STATEMENT_BYTES.
    """
    require_permission(principal, Permission.RECONCILIATION_WRITE)
    if len(content) > MAX_STATEMENT_BYTES:
        raise ValidationError(f"The file is too large: the limit is {MAX_STATEMENT_BYTES} bytes.")
    account = BankAccount.objects.select_for_update().get(pk=bank_account.pk)
    digest = hashlib.sha256(content).hexdigest()
    existing: BankStatementImport | None = BankStatementImport.objects.filter(
        bank_account=account, file_sha256=digest
    ).first()
    if existing is not None and existing.status != BankStatementImport.Status.FAILED:
        return existing
    row = existing or BankStatementImport(
        tenant_id=require_current_tenant_id(), bank_account=account, file_sha256=digest
    )
    row.mapping = {
        "date_column": account.date_column,
        "amount_column": account.amount_column,
        "description_column": account.description_column,
        "payer_column": account.payer_column,
        "date_format": account.date_format,
    }
    row.status = BankStatementImport.Status.PENDING
    row.error = ""
    row.completed_at = None
    row.rows_read = row.duplicates = row.imported = 0
    # ponytail: the stored file is orphaned if the transaction rolls back after it is written
    # (billing has the same ceiling for PDFs). Upgrade path: delete it on rollback, or store it
    # after commit.
    if existing is None:
        row.file.save(f"{row.pk}.csv", ContentFile(content), save=False)
    row.full_clean()
    row.save()
    # Imported here because the task module imports models that this one also uses.
    from reconciliation.tasks import import_statement

    transaction.on_commit(lambda: import_statement.delay(str(row.tenant_id), str(row.pk)))
    return row


def save_bank_account(principal: Principal | None, bank_account: BankAccount) -> BankAccount:
    """Create or update a bank account of the current tenant.

    The account is validated first, uniqueness of the name within the tenant included, so
    ValidationError is raised for invalid data. An account of another tenant is refused when it
    is saved, because the row's tenant must be the current one.

    Args:
        principal: Acting principal, who needs reconciliation:write or PermissionDenied is
            raised.
        bank_account: Account to persist.

    Returns:
        The saved account.
    """
    require_permission(principal, Permission.RECONCILIATION_WRITE)
    if bank_account.tenant_id is None:
        bank_account.tenant_id = require_current_tenant_id()
    bank_account.full_clean()
    bank_account.save()
    return bank_account


def _pending_run() -> ReconciliationRun | None:
    """Find the pending run of the current tenant, if any.

    Returns:
        The pending run, or None.
    """
    return ReconciliationRun.objects.filter(status=ReconciliationRun.Status.PENDING).first()


def run_reconciliation(principal: Principal | None) -> ReconciliationRun:
    """Request a reconciliation run, or return the one already waiting.

    A tenant has at most one pending run, enforced by a partial unique constraint. When one
    exists it is returned and nothing is enqueued, so repeating the request is harmless. Otherwise
    a run is stored with the default engine configuration and version and the requesting user,
    and once the transaction commits the task reconcile_run is enqueued. A request that loses a
    race to a concurrent one hits the constraint inside a savepoint and returns the winner's run.

    Args:
        principal: Acting principal, who needs reconciliation:write or PermissionDenied is
            raised.

    Returns:
        The pending run, new or already existing.

    Raises:
        IntegrityError: When the insert failed on a constraint other than the single pending run.
    """
    require_permission(principal, Permission.RECONCILIATION_WRITE)
    if (pending := _pending_run()) is not None:
        return pending
    # Imported here because the task module imports models that this one also uses.
    from reconciliation.tasks import reconcile_run

    try:
        with transaction.atomic():
            run: ReconciliationRun = ReconciliationRun.objects.create(
                config=engine.default_config(),
                engine_version=engine.engine_version(),
                requested_by_id=principal.user_id if principal else None,
            )
            transaction.on_commit(lambda: reconcile_run.delay(str(run.tenant_id), str(run.pk)))
    except IntegrityError:
        if (winner := _pending_run()) is None:
            raise
        return winner
    return run


def _lock_proposal_rows(
    proposal: MatchProposal,
) -> tuple[MatchProposal, list[BankTransaction], list[Invoice]]:
    """Lock a proposal and the rows it links, in an order that cannot deadlock.

    Invoices are locked by primary key, as record_payment does, and then sorted by due date.

    Args:
        proposal: Proposal to lock.

    Returns:
        The locked proposal, its transactions by value date then key, and its invoices by due
        date then key.
    """
    locked = MatchProposal.objects.select_for_update().get(pk=proposal.pk)
    txs = list(
        BankTransaction.objects.select_for_update(of=("self",))
        .select_related("bank_account")
        .filter(
            pk__in=MatchProposalTransaction.objects.filter(proposal=locked).values("transaction_id")
        )
        .order_by("value_date", "pk")
    )
    invoices = list(
        Invoice.objects.select_for_update()
        .filter(pk__in=MatchProposalInvoice.objects.filter(proposal=locked).values("invoice_id"))
        .order_by("pk")
    )
    invoices.sort(key=lambda row: (row.due_date is None, row.due_date, row.pk))
    return locked, txs, invoices


@transaction.atomic
def _apply_proposal(principal: Principal | None, proposal: MatchProposal) -> list[Payment]:
    """Record the bank payments of a pending proposal and mark it applied.

    Runs in a savepoint, so any error leaves nothing written and the caller's transaction
    usable. The proposal, its transactions and its invoices are locked, and balances are read
    after the lock. Each transaction's amount is allocated to the invoices in due date order,
    at most the balance still due on each, counting what earlier payments of this proposal
    already took. One to one and one to many proposals give one payment with all allocations;
    many to one gives one payment per transaction. A transaction with nothing left to allocate
    still gets its payment, unallocated, so the bank money is accounted for. Each transaction is
    linked to its payment, and the event reconciliation.proposal_applied is recorded.

    Args:
        principal: Acting principal, who needs reconciliation:write and invoices:write, or
            PermissionDenied is raised. Without a user or token the actor is the system.
        proposal: Proposal to apply, read again under lock.

    Returns:
        The payments recorded, in transaction order.

    Raises:
        InvalidTransition: When the proposal is not pending, a transaction already has a
            payment, or an invoice is not a payable invoice with a balance due. Errors of
            record_payment propagate, for example ValidationError on a currency mismatch.
    """
    require_permission(principal, Permission.RECONCILIATION_WRITE)
    locked, txs, invoices = _lock_proposal_rows(proposal)
    if locked.state != MatchProposal.State.PENDING:
        raise InvalidTransition(f"the proposal is {locked.state}, it cannot be applied")
    if any(tx.payment_id is not None for tx in txs):
        raise InvalidTransition("a transaction of the proposal already has a payment")
    remaining = {row.pk: row.balance_due() for row in invoices}
    for row in invoices:
        if (
            row.kind != Invoice.Kind.INVOICE
            or row.status not in Invoice.PAYABLE_STATUSES
            or remaining[row.pk] <= 0
        ):
            raise InvalidTransition(
                f"an invoice of the proposal is {row.status} or has no balance due"
            )
    created: list[Payment] = []
    for tx in txs:
        left = tx.amount
        allocations: list[tuple[Invoice, int]] = []
        for row in invoices:
            cents = min(left, remaining[row.pk])
            if cents > 0:
                allocations.append((row, cents))
                left -= cents
                remaining[row.pk] -= cents
        payment = record_payment(
            principal,
            Payment(
                amount=tx.amount,
                currency=tx.bank_account.currency,
                value_date=tx.value_date,
                source=Payment.Source.BANK,
                reference=tx.description[:200],
            ),
            allocations,
        )
        tx.payment = payment
        tx.save(update_fields=["payment"])
        created.append(payment)
    locked.state = MatchProposal.State.APPLIED
    locked.decided_at = timezone.now()
    locked.decided_by_id = principal.user_id if principal else None
    locked.save(update_fields=["state", "decided_at", "decided_by"])
    record_event(
        principal,
        "reconciliation.proposal_applied",
        "match_proposal",
        locked.pk,
        {"state": MatchProposal.State.PENDING},
        {"state": MatchProposal.State.APPLIED, "payment_ids": [p.pk for p in created]},
    )
    return created
