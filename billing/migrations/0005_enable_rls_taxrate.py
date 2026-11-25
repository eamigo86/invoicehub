"""Protect the tax rate table with Row-Level Security."""

from django.db import migrations

from tenancy.operations import EnableRowLevelSecurity


class Migration(migrations.Migration):
    """Enables and forces tenant isolation on billing_taxrate."""

    dependencies = [("billing", "0004_tax_rate")]

    operations = [EnableRowLevelSecurity("TaxRate")]
