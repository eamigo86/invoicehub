"""Protect the invoice email table with Row-Level Security."""

from django.db import migrations

from tenancy.operations import EnableRowLevelSecurity


class Migration(migrations.Migration):
    """Enables and forces tenant isolation on the invoice email table."""

    dependencies = [("billing", "0020_invoice_email")]

    operations = [EnableRowLevelSecurity("InvoiceEmail")]
