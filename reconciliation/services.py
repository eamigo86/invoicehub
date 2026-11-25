"""Reconciliation services: the write path for bank statement imports.

Views, the admin and later the API call these functions. Cross-tenant references are refused
the same way as in billing: Row-Level Security hides the rows of other tenants, so an account
of another tenant is not found.
"""

import hashlib
import uuid
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from billing.models import Invoice, Payment
from billing.services import (
    InvalidTransition,
    _payment_snapshot,
    record_event,
    record_payment,
    reverse_payment,
)
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
def _apply_proposal(
    principal: Principal | None,
    proposal: MatchProposal,
    *,
    reassigned_from: dict[str, Any] | None = None,
) -> list[Payment]:
    """Record the bank payments of a pending proposal and mark it applied or reassigned.

    Runs in a savepoint, so any error leaves nothing written and the caller's transaction
    usable. The proposal, its transactions and its invoices are locked, and balances are read
    after the lock. Each transaction's amount is allocated to the invoices in due date order,
    at most the balance still due on each, counting what earlier payments of this proposal
    already took. One to one and one to many proposals give one payment with all allocations;
    many to one gives one payment per transaction. A transaction with nothing left to allocate
    still gets its payment, unallocated, so the bank money is accounted for. Each transaction is
    linked to its payment. The final state is applied and the event is
    reconciliation.proposal_applied, unless reassigned_from is given: then the proposal ends
    reassigned and the event is reconciliation.proposal_reassigned, whose before state lists
    the engine's kind and invoice ids, and whose after state lists the kind and invoices
    linked now.

    Args:
        principal: Acting principal, who needs reconciliation:write and invoices:write, or
            PermissionDenied is raised. Without a user or token the actor is the system.
        proposal: Proposal to apply, read again under lock.
        reassigned_from: The engine's kind and invoice ids, recorded in the before state, when
            the user chose other invoices, or None for a plain application.

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
    final = (
        MatchProposal.State.APPLIED if reassigned_from is None else MatchProposal.State.REASSIGNED
    )
    locked.state = final
    locked.decided_at = timezone.now()
    locked.decided_by_id = principal.user_id if principal else None
    locked.save(update_fields=["state", "decided_at", "decided_by"])
    before: dict[str, Any] = {"state": MatchProposal.State.PENDING}
    after: dict[str, Any] = {"state": final, "payment_ids": [p.pk for p in created]}
    if reassigned_from is not None:
        before.update(reassigned_from)
        after["kind"] = locked.kind
        after["invoice_ids"] = [row.pk for row in invoices]
    record_event(
        principal,
        "reconciliation.proposal_applied"
        if reassigned_from is None
        else "reconciliation.proposal_reassigned",
        "match_proposal",
        locked.pk,
        before,
        after,
    )
    return created


def _link_ids(
    model: type[MatchProposalTransaction] | type[MatchProposalInvoice],
    proposal: MatchProposal,
    column: str,
) -> list[uuid.UUID]:
    """List the ids a proposal links, in key order.

    Args:
        model: Link model to read.
        proposal: Proposal whose links are listed.
        column: Name of the linked id column.

    Returns:
        The linked ids.
    """
    return list(
        model.objects.filter(proposal=proposal).order_by(column).values_list(column, flat=True)
    )


@transaction.atomic
def accept_proposal(principal: Principal | None, proposal: MatchProposal) -> list[Payment]:
    """Apply a pending proposal on behalf of the user who reviewed it.

    The engine status does not matter: suggested and ambiguous proposals are accepted the same
    way as any other pending one. The work is _apply_proposal with the user's principal, so the
    payments, the applied state, decided_by and the audit event name the user. Both
    reconciliation:write and invoices:write are checked first, so a refusal writes nothing.

    Args:
        principal: Acting principal, who needs reconciliation:write and invoices:write or
            PermissionDenied is raised.
        proposal: Proposal to accept, read again under lock.

    Returns:
        The payments recorded, in transaction order.
    """
    require_permission(principal, Permission.RECONCILIATION_WRITE)
    require_permission(principal, Permission.INVOICES_WRITE)
    return _apply_proposal(principal, proposal)


@transaction.atomic
def reject_proposal(principal: Principal | None, proposal: MatchProposal) -> MatchProposal:
    """Reject a pending proposal, which frees its transactions and invoices for the next run.

    The proposal is locked and must be pending. Its state, the deciding user and the time are
    stored and the event reconciliation.proposal_rejected records the state before and after.
    No payment is recorded.

    Args:
        principal: Acting principal, who needs reconciliation:write or PermissionDenied is
            raised.
        proposal: Proposal to reject, read again under lock.

    Returns:
        The rejected proposal.

    Raises:
        InvalidTransition: When the proposal is not pending.
    """
    require_permission(principal, Permission.RECONCILIATION_WRITE)
    locked: MatchProposal = MatchProposal.objects.select_for_update().get(pk=proposal.pk)
    if locked.state != MatchProposal.State.PENDING:
        raise InvalidTransition(f"the proposal is {locked.state}, it cannot be rejected")
    locked.state = MatchProposal.State.REJECTED
    locked.decided_at = timezone.now()
    locked.decided_by_id = principal.user_id if principal else None
    locked.save(update_fields=["state", "decided_at", "decided_by"])
    record_event(
        principal,
        "reconciliation.proposal_rejected",
        "match_proposal",
        locked.pk,
        {
            "state": MatchProposal.State.PENDING,
            "transaction_ids": _link_ids(MatchProposalTransaction, locked, "transaction_id"),
            "invoice_ids": _link_ids(MatchProposalInvoice, locked, "invoice_id"),
        },
        {"state": MatchProposal.State.REJECTED},
    )
    return locked


@transaction.atomic
def reassign_proposal(
    principal: Principal | None, proposal: MatchProposal, invoices: list[Invoice]
) -> list[Payment]:
    """Keep the bank transactions of a pending proposal and pay other invoices with them.

    The proposal is locked and must be pending. The invoice list must not be empty or repeat an
    invoice, and every id must resolve under Row-Level Security, so an invoice of another tenant
    is refused. Several transactions together with several invoices are not supported. The
    proposal links are replaced by the chosen invoices, kind follows the new cardinality, and
    the payments are recorded by _apply_proposal, which also refuses invoices that are not
    payable or are in another currency. Everything runs in one transaction, so any refusal
    leaves links, payments and state untouched. The proposal ends reassigned and the event
    reconciliation.proposal_reassigned records the engine's invoice ids before and the chosen
    ones after, with the kinds and the payments. No proposal_applied event is recorded. Score
    and breakdown are not touched: they keep describing the engine's original pairing, not the
    chosen one. An invoice linked to another pending proposal is refused, as runs treat it as
    held. A chosen invoice may end up with no allocation when the transactions are used up
    before it.

    Args:
        principal: Acting principal, who needs reconciliation:write and invoices:write or
            PermissionDenied is raised.
        proposal: Proposal to reassign, read again under lock.
        invoices: Invoices the user chose, of which only the primary keys are used.

    Returns:
        The payments recorded, in transaction order.

    Raises:
        InvalidTransition: When the proposal is not pending. Errors of _apply_proposal
            propagate for invoices that are not payable.
        ValidationError: When the list is empty, repeats an invoice, would make a many to many
            proposal, has an invoice held by another pending proposal, or has an invoice in
            another currency than the payments.
        Invoice.DoesNotExist: When an invoice is not visible to the current tenant.
    """
    require_permission(principal, Permission.RECONCILIATION_WRITE)
    require_permission(principal, Permission.INVOICES_WRITE)
    locked = MatchProposal.objects.select_for_update().get(pk=proposal.pk)
    if locked.state != MatchProposal.State.PENDING:
        raise InvalidTransition(f"the proposal is {locked.state}, it cannot be reassigned")
    ids = [row.pk for row in invoices]
    if not ids:
        raise ValidationError("Choose at least one invoice.")
    if len(set(ids)) != len(ids):
        raise ValidationError("An invoice was chosen more than once.")
    if Invoice.objects.filter(pk__in=ids).count() != len(ids):
        raise Invoice.DoesNotExist("An invoice was not found.")
    tx_count = MatchProposalTransaction.objects.filter(proposal=locked).count()
    if tx_count > 1 and len(ids) > 1:
        raise ValidationError("Several transactions cannot be matched to several invoices.")
    if (
        MatchProposalInvoice.objects.filter(
            invoice_id__in=ids, proposal__state=MatchProposal.State.PENDING
        )
        .exclude(proposal=locked)
        .exists()
    ):
        raise ValidationError("An invoice is already held by another pending proposal.")
    engine = {
        "kind": locked.kind,
        "invoice_ids": _link_ids(MatchProposalInvoice, locked, "invoice_id"),
    }
    MatchProposalInvoice.objects.filter(proposal=locked).delete()
    for invoice_id in ids:
        MatchProposalInvoice.objects.create(proposal=locked, invoice_id=invoice_id)
    locked.kind = (
        MatchProposal.Kind.MANY_TO_ONE
        if tx_count > 1
        else MatchProposal.Kind.ONE_TO_ONE
        if len(ids) == 1
        else MatchProposal.Kind.ONE_TO_MANY
    )
    locked.save(update_fields=["kind"])
    return _apply_proposal(principal, locked, reassigned_from=engine)


@transaction.atomic
def undo_proposal(principal: Principal | None, proposal: MatchProposal) -> MatchProposal:
    """Undo an applied or reassigned proposal by reversing the bank payments it recorded.

    Both reconciliation:write and invoices:write are checked first. The proposal and its
    transactions are locked in the order _apply_proposal uses, and the state must be applied or
    reassigned. The payment of each transaction goes through reverse_payment, which restores the
    invoices and clears the link; a transaction whose payment is already gone is skipped. The
    proposal ends undone with the undoing user and the time, which is not a held state, so the
    transactions and invoices are free for the next run. The event
    reconciliation.proposal_undone records the state, payment ids and invoice ids before, and
    the state after. The before state also keeps the previous decision and every payment reversed
    with its allocations, the only trace of a payment with none. There is no closed period yet,
    so an undo is always allowed. Everything runs in one transaction, so any refusal, including
    one of reverse_payment, writes nothing.

    Args:
        principal: Acting principal, who needs reconciliation:write and invoices:write or
            PermissionDenied is raised.
        proposal: Proposal to undo, read again under lock.

    Returns:
        The undone proposal.

    Raises:
        InvalidTransition: When the proposal is not applied or reassigned. Errors of
            reverse_payment propagate, for example for a voided invoice.
    """
    require_permission(principal, Permission.RECONCILIATION_WRITE)
    require_permission(principal, Permission.INVOICES_WRITE)
    locked, txs, _ = _lock_proposal_rows(proposal)
    if locked.state not in (MatchProposal.State.APPLIED, MatchProposal.State.REASSIGNED):
        raise InvalidTransition(f"the proposal is {locked.state}, it cannot be undone")
    # Locked up front, in key order: a concurrent direct reverse_payment holds its payment and
    # then waits for an invoice, so taking invoices first from payment one could deadlock with it.
    payments = list(
        Payment.objects.select_for_update()
        .filter(pk__in=[tx.payment_id for tx in txs if tx.payment_id is not None])
        .order_by("pk")
    )
    details = [_payment_snapshot(payment) for payment in payments]
    before: dict[str, Any] = {
        "state": locked.state,
        "decided_by_id": locked.decided_by_id,
        "decided_at": locked.decided_at.isoformat() if locked.decided_at else None,
        "payment_ids": [payment.pk for payment in payments],
        "invoice_ids": sorted({a["invoice_id"] for d in details for a in d["allocations"]}),
        "payments": details,
    }
    for payment in payments:
        reverse_payment(principal, payment)
    locked.state = MatchProposal.State.UNDONE
    locked.decided_at = timezone.now()
    locked.decided_by_id = principal.user_id if principal else None
    locked.save(update_fields=["state", "decided_at", "decided_by"])
    record_event(
        principal,
        "reconciliation.proposal_undone",
        "match_proposal",
        locked.pk,
        before,
        {"state": MatchProposal.State.UNDONE},
    )
    return locked
