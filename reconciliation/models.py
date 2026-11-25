"""Reconciliation models: bank accounts, uploaded statements and their transactions."""

import uuid

from django.contrib.postgres.functions import RandomUUID
from django.db import models

from core.models import Currency
from reconciliation.parsing import DESCRIPTION_MAX_LENGTH, PAYER_MAX_LENGTH
from tenancy.models import TenantModel


class BankAccount(TenantModel):
    """A bank whose CSV column mapping is saved for the next upload.

    Attributes:
        name: Label shown to users, unique within the tenant.
        account_identifier: The account as printed on statements, for example its last four
            digits. It is part of the transaction deduplication hash.
        currency: Currency of every transaction of the account.
        date_column: CSV column holding the value date.
        amount_column: CSV column holding the signed amount.
        description_column: CSV column holding the description.
        payer_column: CSV column holding the payer, empty when the bank has none.
        date_format: strptime format of the date column.
    """

    id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, db_default=RandomUUID(), editable=False
    )
    name = models.CharField(max_length=100)
    account_identifier = models.CharField(max_length=34)
    currency = models.CharField(max_length=3, choices=Currency, default=Currency.USD)
    date_column = models.CharField(max_length=100)
    amount_column = models.CharField(max_length=100)
    description_column = models.CharField(max_length=100)
    payer_column = models.CharField(max_length=100, blank=True)
    date_format = models.CharField(max_length=20, default="%m/%d/%Y")

    class Meta:
        """Model options."""

        constraints = [
            models.UniqueConstraint(fields=["tenant", "name"], name="bankaccount_tenant_name_uniq")
        ]

    def __str__(self) -> str:
        """Return the account name.

        Returns:
            The display name of the account.
        """
        return self.name


def bank_import_path(instance: "BankStatementImport", filename: str) -> str:
    """Build the storage path of an uploaded bank statement.

    Args:
        instance: Import the file belongs to.
        filename: Name of the uploaded file, which is ignored.

    Returns:
        The path tenants/<tenant_id>/bank_imports/<id>.csv.
    """
    return f"tenants/{instance.tenant_id}/bank_imports/{instance.pk}.csv"


class BankStatementImport(TenantModel):
    """One uploaded CSV statement and the outcome of importing it.

    Attributes:
        bank_account: Account the statement belongs to.
        file: The stored CSV.
        file_sha256: SHA-256 of the file, the upload idempotency key.
        mapping: Snapshot of the account columns and date format taken at upload.
        status: Whether the import is pending, completed or failed.
        rows_read: Data rows found in the file.
        duplicates: Rows skipped because the transaction already existed.
        imported: Transactions created.
        error: Failure message, empty unless the import failed.
        created_at: When the file was uploaded.
        completed_at: When the import finished, empty until then.
    """

    class Status(models.TextChoices):
        """The import states."""

        PENDING = "pending"
        COMPLETED = "completed"
        FAILED = "failed"

    id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, db_default=RandomUUID(), editable=False
    )
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name="imports")
    file = models.FileField(upload_to=bank_import_path, max_length=200)
    file_sha256 = models.CharField(max_length=64)
    mapping = models.JSONField()
    status = models.CharField(max_length=10, choices=Status, default=Status.PENDING)
    rows_read = models.PositiveIntegerField(default=0)
    duplicates = models.PositiveIntegerField(default=0)
    imported = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Model options."""

        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "bank_account", "file_sha256"], name="bankimport_tenant_sha_uniq"
            )
        ]

    def __str__(self) -> str:
        """Describe the import by account and status.

        Returns:
            The account name and the status.
        """
        return f"{self.bank_account} ({self.status})"


class BankTransaction(TenantModel):
    """A bank statement row, kept once however many files contain it.

    Attributes:
        bank_account: Account the transaction belongs to.
        statement_import: Import that first brought the transaction in.
        value_date: Day the bank booked the transaction.
        amount: Signed amount in cents, positive for credits and negative for debits.
        description: Text printed by the bank.
        payer: Payer printed by the bank, if any.
        dedupe_hash: SHA-256 identifying the transaction across overlapping statements.
        created_at: When the transaction was imported.
    """

    id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, db_default=RandomUUID(), editable=False
    )
    bank_account = models.ForeignKey(
        BankAccount, on_delete=models.PROTECT, related_name="transactions"
    )
    statement_import = models.ForeignKey(
        BankStatementImport, on_delete=models.PROTECT, related_name="transactions"
    )
    value_date = models.DateField()
    amount = models.BigIntegerField()
    description = models.CharField(max_length=DESCRIPTION_MAX_LENGTH)
    payer = models.CharField(max_length=PAYER_MAX_LENGTH, blank=True)
    dedupe_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Model options."""

        ordering = ["value_date", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "dedupe_hash"], name="banktx_tenant_hash_uniq"
            )
        ]

    def __str__(self) -> str:
        """Describe the transaction by date, amount and description.

        Returns:
            The value date, the amount in cents and the description.
        """
        return f"{self.value_date} {self.amount} {self.description}"
