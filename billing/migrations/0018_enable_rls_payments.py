"""Protect the payment tables with Row-Level Security."""

from django.db import migrations

from tenancy.operations import EnableRowLevelSecurity


class Migration(migrations.Migration):
    """Enables and forces tenant isolation on the payment and allocation tables."""

    dependencies = [("billing", "0017_payments")]

    operations = [EnableRowLevelSecurity("Payment"), EnableRowLevelSecurity("PaymentAllocation")]
