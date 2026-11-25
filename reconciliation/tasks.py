"""Celery tasks of the reconciliation app."""

from django.utils import timezone

from config.celery import app
from reconciliation.models import BankAccount, BankStatementImport, BankTransaction
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
