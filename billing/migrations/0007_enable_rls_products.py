"""Protect the product tables with Row-Level Security."""

from django.db import migrations

from tenancy.operations import EnableRowLevelSecurity


class Migration(migrations.Migration):
    """Enables and forces tenant isolation on billing_product and billing_producttaxrate."""

    dependencies = [("billing", "0006_products")]

    operations = [EnableRowLevelSecurity("Product"), EnableRowLevelSecurity("ProductTaxRate")]
