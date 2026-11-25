"""Protect the audit event table with Row-Level Security."""

from django.db import migrations

from tenancy.operations import EnableRowLevelSecurity


class Migration(migrations.Migration):
    """Enables and forces tenant isolation on billing_auditevent."""

    dependencies = [("billing", "0008_audit_event")]

    operations = [EnableRowLevelSecurity("AuditEvent")]
