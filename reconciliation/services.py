"""Reconciliation services: the write path for bank statement imports.

Views, the admin and later the API call these functions. Cross-tenant references are refused
the same way as in billing: Row-Level Security hides the rows of other tenants, so an account
of another tenant is not found.
"""

import hashlib

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction

from reconciliation.models import BankAccount, BankStatementImport
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
