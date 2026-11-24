"""Billing models."""

from django.db import models

from tenancy.models import TenantModel


class Customer(TenantModel):
    """A person or company that is invoiced by a tenant.

    Its data is copied onto an invoice when the invoice is issued, so editing a customer
    never alters issued invoices.
    """

    name = models.CharField(max_length=200)
    tax_id = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)

    def __str__(self) -> str:
        """Return the customer name.

        Returns:
            The display name of the customer.
        """
        return self.name
