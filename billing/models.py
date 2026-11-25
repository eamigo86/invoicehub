"""Billing models."""

import uuid

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.functions import RandomUUID
from django.core.serializers.json import DjangoJSONEncoder
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from core.models import Currency, PostalAddress
from tenancy.models import TenantModel


class Customer(TenantModel, PostalAddress):
    """A person or company that is invoiced by a tenant.

    Its data is copied onto an invoice when the invoice is issued, so editing a customer
    never alters issued invoices.
    """

    name = models.CharField(max_length=200)
    tax_id = models.CharField(max_length=50, blank=True)  # optional EIN
    emails = ArrayField(models.EmailField(), default=list, blank=True)

    def __str__(self) -> str:
        """Return the customer name.

        Returns:
            The display name of the customer.
        """
        return self.name


class TaxRate(TenantModel):
    """A tax percentage the tenant configures, for example a state or local sales tax.

    InvoiceHub only calculates the rates the tenant enters; it knows no tax types.

    Attributes:
        name: Label shown to users, unique within the tenant.
        rate_bp: Rate in basis points, so 8.25 percent is 825.
    """

    name = models.CharField(max_length=100)
    rate_bp = models.PositiveIntegerField(validators=[MaxValueValidator(10000)])

    class Meta:
        """Model options."""

        constraints = [
            models.UniqueConstraint(fields=["tenant", "name"], name="taxrate_tenant_name_uniq")
        ]

    def __str__(self) -> str:
        """Return the tax rate name.

        Returns:
            The display name of the tax rate.
        """
        return self.name


class Product(TenantModel):
    """A catalog entry that prefills invoice lines; a line can also be free-form.

    Attributes:
        name: Label shown to users.
        description: Optional longer text.
        unit_price: Price in cents, in the default currency of the tenant.
        default_taxes: Tax rates a new line of this product starts with.
    """

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    unit_price = models.BigIntegerField(
        validators=[MinValueValidator(0)],
        help_text="Price in cents, for example 1999 for 19.99.",
    )
    default_taxes = models.ManyToManyField(TaxRate, through="ProductTaxRate", blank=True)

    def __str__(self) -> str:
        """Return the product name.

        Returns:
            The display name of the product.
        """
        return self.name


class ProductTaxRate(TenantModel):
    """Link between a product and one of its default tax rates.

    An explicit through model, because an implicit one would carry no tenant.
    """

    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    tax_rate = models.ForeignKey(TaxRate, on_delete=models.CASCADE)

    class Meta:
        """Model options."""

        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "product", "tax_rate"], name="producttaxrate_tenant_pair_uniq"
            )
        ]


class AuditEvent(TenantModel):
    """One entry of the audit log: who did what to which entity, and when.

    The log is append-only: rows are written by record_event and never updated or deleted by
    the application. The actor references are cleared if the user or token is deleted, while
    actor_kind keeps saying what kind of actor it was.

    Attributes:
        actor_kind: Whether a user, an API token, the system or the AI acted.
        actor_user: The acting user, when actor_kind is user.
        actor_token: The acting API token, when actor_kind is token.
        action: What happened, for example invoice.issued.
        entity: Kind of the entity acted on, for example invoice.
        entity_id: Primary key of that entity.
        before: Relevant state before the action, if any.
        after: Relevant state after the action, if any.
        created_at: When the event was recorded.
    """

    # ponytail: append-only is enforced by having no write path other than record_event;
    # revoking UPDATE and DELETE on the table from the app role is the upgrade path.

    class ActorKind(models.TextChoices):
        """The kinds of actor an event can be attributed to."""

        USER = "user"
        TOKEN = "token"
        SYSTEM = "system"
        AI = "ai"

    actor_kind = models.CharField(max_length=10, choices=ActorKind)
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    actor_token = models.ForeignKey(
        "tenancy.ApiToken", null=True, on_delete=models.SET_NULL, related_name="+"
    )
    action = models.CharField(max_length=100)
    entity = models.CharField(max_length=50)
    entity_id = models.UUIDField()
    before = models.JSONField(null=True, encoder=DjangoJSONEncoder)
    after = models.JSONField(null=True, encoder=DjangoJSONEncoder)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        """Model options."""

        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        """Describe the event by action and entity.

        Returns:
            A label such as invoice.issued on invoice 1c2d.
        """
        return f"{self.action} on {self.entity} {self.entity_id}"


class Invoice(TenantModel):
    """An invoice or credit note, drafted and then issued; issued invoices are immutable.

    Overdue is never stored: it is derived from the due date and the balance. The identifier
    is a UUID, so the audit log can reference it.

    Attributes:
        id: Primary key.
        kind: Whether this is an invoice or a credit note.
        status: Lifecycle state of the document.
        customer: Who is invoiced.
        original: The invoice a credit note corrects; empty for an invoice.
        currency: Currency of every amount on the document.
        series: Numbering series the invoice was numbered from, set when issuing.
        number: Position in the series within the year, set when issuing.
        year: Year of the numbering, set when issuing.
        reference: Display number such as INV-2026-00001, stored when issuing so that a later
            prefix change never alters it.
        issuer: Frozen copy of the issuer details, taken when issuing.
        customer_details: Frozen copy of the customer details, taken when issuing.
        issued_at: Moment the invoice was issued.
        issue_date: Day the invoice was issued, set when issuing.
        due_date: Day payment is due, set when issuing.
        notes: Free text shown on the document.
        subtotal: Sum of the line nets, in cents.
        tax_total: Sum of every line tax, in cents.
        total: Subtotal plus tax total, in cents.
        created_at: When the draft was created.
        updated_at: When the document last changed.
    """

    class Kind(models.TextChoices):
        """The kinds of document."""

        INVOICE = "invoice"
        CREDIT_NOTE = "credit_note"

    class Status(models.TextChoices):
        """The stored lifecycle states."""

        DRAFT = "draft"
        ISSUED = "issued"
        SENT = "sent"
        PARTIALLY_PAID = "partially_paid"
        PAID = "paid"
        VOIDED = "voided"

    id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, db_default=RandomUUID(), editable=False
    )
    kind = models.CharField(max_length=12, choices=Kind, default=Kind.INVOICE)
    status = models.CharField(max_length=14, choices=Status, default=Status.DRAFT)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    original = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="credit_notes"
    )
    currency = models.CharField(max_length=3, choices=Currency)
    series = models.ForeignKey(
        "InvoiceSeries", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    number = models.PositiveIntegerField(null=True, blank=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    reference = models.CharField(max_length=40, blank=True)
    issuer = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)
    customer_details = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)
    issued_at = models.DateTimeField(null=True, blank=True)
    issue_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    subtotal = models.BigIntegerField(default=0)
    tax_total = models.BigIntegerField(default=0)
    total = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Model options."""

        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "series", "year", "number"], name="invoice_tenant_number_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(kind="credit_note", original__isnull=False)
                | models.Q(kind="invoice", original__isnull=True),
                name="invoice_original_matches_kind",
            ),
        ]

    def __str__(self) -> str:
        """Describe the invoice by customer and status.

        Returns:
            A label such as Acme draft invoice.
        """
        return f"{self.customer} {self.status} {self.kind}"


class InvoiceSeries(TenantModel):
    """The gap-free numbering counter of one kind of document within a tenant.

    Issuing locks the row, hands out next_number and increments it in the same transaction, and
    the counter restarts at 1 when the issue year is later than year.

    Attributes:
        kind: Kind of document the series numbers; a tenant has one series per kind.
        prefix: Text that starts the reference, for example INV- or CN-.
        year: Year the counter currently runs in.
        next_number: Number the next issued document receives.
    """

    kind = models.CharField(max_length=12, choices=Invoice.Kind)
    prefix = models.CharField(max_length=20)
    year = models.PositiveSmallIntegerField()
    next_number = models.PositiveIntegerField(default=1)

    class Meta:
        """Model options."""

        verbose_name_plural = "invoice series"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "kind"], name="invoiceseries_tenant_kind_uniq"
            )
        ]

    def __str__(self) -> str:
        """Describe the series by prefix and kind.

        Returns:
            A label such as INV- (invoice).
        """
        return f"{self.prefix} ({self.kind})"


class InvoiceLine(TenantModel):
    """One priced line of an invoice, with amounts stored in cents.

    Attributes:
        invoice: Invoice the line belongs to.
        position: Order of the line within the invoice.
        product: Catalog entry the line was prefilled from, if any; protected against deletion.
        description: What is being charged.
        quantity: Amount of units, with up to three decimals.
        unit_price: Price of one unit in cents.
        discount_bp: Line discount in basis points.
        net: Amount after discount and before tax, in cents.
        total: Net plus the line taxes, in cents.
    """

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    position = models.PositiveSmallIntegerField()
    product = models.ForeignKey(Product, null=True, blank=True, on_delete=models.PROTECT)
    description = models.CharField(max_length=500)
    quantity = models.DecimalField(
        max_digits=12, decimal_places=3, validators=[MinValueValidator(0)]
    )
    unit_price = models.BigIntegerField(validators=[MinValueValidator(0)])
    discount_bp = models.PositiveIntegerField(default=0, validators=[MaxValueValidator(10000)])
    net = models.BigIntegerField(default=0)
    total = models.BigIntegerField(default=0)

    class Meta:
        """Model options."""

        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "invoice", "position"], name="invoiceline_tenant_position_uniq"
            )
        ]

    def __str__(self) -> str:
        """Return the line description.

        Returns:
            The description of the line.
        """
        return self.description


class InvoiceLineTax(TenantModel):
    """A tax applied to a line, with the name and rate copied from the configured tax rate.

    The snapshot keeps the invoice stable when the tax rate is later edited. A rate used by a line
    tax cannot be deleted.

    Attributes:
        line: Line the tax applies to.
        tax_rate: The configured rate it came from, protected against deletion.
        name: Name of the rate when the tax was calculated.
        rate_bp: Rate in basis points when the tax was calculated.
        amount: Tax on the line net, in cents.
    """

    line = models.ForeignKey(InvoiceLine, on_delete=models.CASCADE, related_name="taxes")
    tax_rate = models.ForeignKey(TaxRate, null=True, blank=True, on_delete=models.PROTECT)
    name = models.CharField(max_length=100)
    rate_bp = models.PositiveIntegerField()
    amount = models.BigIntegerField()

    def __str__(self) -> str:
        """Describe the tax by name and rate.

        Returns:
            A label such as Austin 825 bp.
        """
        return f"{self.name} {self.rate_bp} bp"
