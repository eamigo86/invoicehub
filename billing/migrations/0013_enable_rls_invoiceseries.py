"""Protect the invoice series table with Row-Level Security."""

from django.db import migrations

from tenancy.operations import EnableRowLevelSecurity


class Migration(migrations.Migration):
    """Enables and forces tenant isolation on the invoice series table."""

    dependencies = [("billing", "0012_invoice_numbering")]

    operations = [EnableRowLevelSecurity("InvoiceSeries")]
