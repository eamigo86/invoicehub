"""Models and choices shared by several apps."""

from django.db import models


class Currency(models.TextChoices):
    """Currencies an issuer can invoice in."""

    USD = "USD"
    CAD = "CAD"
    MXN = "MXN"


class PostalAddress(models.Model):
    """Abstract postal address, defaulting to the United States."""

    address_line1 = models.CharField(max_length=200, blank=True)
    address_line2 = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=2, default="US")

    class Meta:
        """Model options."""

        abstract = True
