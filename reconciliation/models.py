"""Reconciliation models: bank accounts, uploaded statements and their transactions."""

import uuid

from django.conf import settings
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


class ReconciliationRun(TenantModel):
    """One execution of the matching engine and the metrics it produced.

    Attributes:
        status: Whether the run is pending, completed or failed.
        config: The match configuration values used, as plain JSON.
        engine_version: Version of the matching engine that ran.
        requested_by: User who started the run, empty for system runs.
        transactions_considered: Bank transactions given to the engine.
        invoices_considered: Open invoices given to the engine.
        auto_count: Proposals the engine marked auto.
        suggested_count: Proposals the engine marked suggested.
        ambiguous_count: Proposals the engine marked ambiguous.
        unmatched_transactions: Transactions with no proposal.
        unmatched_invoices: Invoices with no proposal.
        duration_ms: Wall time of the run in milliseconds.
        error: Failure message, empty unless the run failed.
        created_at: When the run was requested.
        completed_at: When the run finished, empty until then.
    """

    class Status(models.TextChoices):
        """The run states."""

        PENDING = "pending"
        COMPLETED = "completed"
        FAILED = "failed"

    id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, db_default=RandomUUID(), editable=False
    )
    status = models.CharField(max_length=10, choices=Status, default=Status.PENDING)
    config = models.JSONField()
    engine_version = models.CharField(max_length=30, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    transactions_considered = models.PositiveIntegerField(default=0)
    invoices_considered = models.PositiveIntegerField(default=0)
    auto_count = models.PositiveIntegerField(default=0)
    suggested_count = models.PositiveIntegerField(default=0)
    ambiguous_count = models.PositiveIntegerField(default=0)
    unmatched_transactions = models.PositiveIntegerField(default=0)
    unmatched_invoices = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Model options."""

        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant"],
                condition=models.Q(status="pending"),
                name="reconrun_one_pending_per_tenant",
            )
        ]

    def __str__(self) -> str:
        """Describe the run by date and status.

        Returns:
            The creation time and the status.
        """
        return f"{self.created_at:%Y-%m-%d %H:%M} ({self.status})"


class MatchProposal(TenantModel):
    """A set of transactions the engine proposes to settle a set of invoices.

    Attributes:
        run: Run that produced the proposal.
        kind: Cardinality, one to one, one to many or many to one.
        engine_status: Status exactly as the engine returned it.
        score: Engine confidence between 0 and 1, not money.
        breakdown: Score per signal, shown to explain the proposal.
        residual: Transactions minus invoices in cents, negative when a fee was deducted.
        state: Where the proposal stands in the review workflow.
        decided_by: User who decided, empty for system decisions.
        decided_at: When the proposal left the pending state.
        created_at: When the proposal was created.
        transactions: The bank transactions it links.
        invoices: The invoices it links.
    """

    class Kind(models.TextChoices):
        """Cardinality of a proposal, as remitmatch spells it."""

        ONE_TO_ONE = "1:1"
        ONE_TO_MANY = "1:N"
        MANY_TO_ONE = "N:1"

    class EngineStatus(models.TextChoices):
        """Status assigned by the engine."""

        AUTO = "auto"
        SUGGESTED = "suggested"
        AMBIGUOUS = "ambiguous"

    class State(models.TextChoices):
        """Review workflow states."""

        PENDING = "pending"
        APPLIED = "applied"
        REJECTED = "rejected"
        REASSIGNED = "reassigned"
        UNDONE = "undone"

    id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, db_default=RandomUUID(), editable=False
    )
    run = models.ForeignKey(ReconciliationRun, on_delete=models.PROTECT, related_name="proposals")
    kind = models.CharField(max_length=3, choices=Kind)
    engine_status = models.CharField(max_length=10, choices=EngineStatus)
    score = models.FloatField()
    breakdown = models.JSONField()
    residual = models.BigIntegerField()
    state = models.CharField(max_length=10, choices=State, default=State.PENDING)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    transactions = models.ManyToManyField(
        BankTransaction, through="MatchProposalTransaction", related_name="proposals"
    )
    invoices = models.ManyToManyField(
        "billing.Invoice", through="MatchProposalInvoice", related_name="+"
    )

    class Meta:
        """Model options."""

        constraints = [
            models.CheckConstraint(
                condition=models.Q(score__gte=0, score__lte=1), name="matchproposal_score_unit"
            )
        ]

    def __str__(self) -> str:
        """Describe the proposal by kind, engine status and score.

        Returns:
            The kind, the engine status and the score.
        """
        return f"{self.kind} {self.engine_status} {self.score:.2f}"


class MatchProposalTransaction(TenantModel):
    """Link between a proposal and one of its bank transactions.

    An explicit through model, because an implicit one would carry no tenant.

    Attributes:
        proposal: The proposal, whose deletion removes the link.
        transaction: The linked transaction, protected against deletion.
    """

    id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, db_default=RandomUUID(), editable=False
    )
    proposal = models.ForeignKey(MatchProposal, on_delete=models.CASCADE)
    transaction = models.ForeignKey(BankTransaction, on_delete=models.PROTECT)

    class Meta:
        """Model options."""

        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "proposal", "transaction"], name="matchproptx_tenant_pair_uniq"
            )
        ]


class MatchProposalInvoice(TenantModel):
    """Link between a proposal and one of its invoices.

    An explicit through model, because an implicit one would carry no tenant.

    Attributes:
        proposal: The proposal, whose deletion removes the link.
        invoice: The linked invoice, protected against deletion.
    """

    id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, db_default=RandomUUID(), editable=False
    )
    proposal = models.ForeignKey(MatchProposal, on_delete=models.CASCADE)
    invoice = models.ForeignKey("billing.Invoice", on_delete=models.PROTECT)

    class Meta:
        """Model options."""

        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "proposal", "invoice"], name="matchpropinv_tenant_pair_uniq"
            )
        ]
