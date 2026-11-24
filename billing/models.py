"""Billing models."""

from django.contrib.postgres.fields import ArrayField
from django.core.validators import MaxValueValidator
from django.db import models

from core.models import PostalAddress
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
