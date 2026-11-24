"""Billing models."""

from django.contrib.postgres.fields import ArrayField
from django.core.validators import MaxValueValidator, MinValueValidator
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

    def __str__(self) -> str:
        """Describe the link by product and tax rate.

        Returns:
            A label such as Widget / Austin.
        """
        return f"{self.product} / {self.tax_rate}"
