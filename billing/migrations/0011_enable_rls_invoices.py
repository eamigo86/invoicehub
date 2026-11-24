"""Protect the invoice, line and line tax tables with Row-Level Security."""

from django.db import migrations

from tenancy.operations import EnableRowLevelSecurity


class Migration(migrations.Migration):
    """Enables and forces tenant isolation on the invoice tables."""

    dependencies = [("billing", "0010_invoices")]

    operations = [
        EnableRowLevelSecurity("Invoice"),
        EnableRowLevelSecurity("InvoiceLine"),
        EnableRowLevelSecurity("InvoiceLineTax"),
    ]
