"""Protect the customer table with Row-Level Security."""

from django.db import migrations

from tenancy.operations import EnableRowLevelSecurity


class Migration(migrations.Migration):
    """Enables and forces tenant isolation on billing_customer."""

    dependencies = [("billing", "0001_initial")]

    operations = [EnableRowLevelSecurity("Customer")]
