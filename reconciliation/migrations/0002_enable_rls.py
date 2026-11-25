"""Protect the reconciliation tables with Row-Level Security."""

from django.db import migrations

from tenancy.operations import EnableRowLevelSecurity


class Migration(migrations.Migration):
    """Enables and forces tenant isolation on the reconciliation tables."""

    dependencies = [("reconciliation", "0001_initial")]

    operations = [
        EnableRowLevelSecurity("BankAccount"),
        EnableRowLevelSecurity("BankStatementImport"),
        EnableRowLevelSecurity("BankTransaction"),
    ]
