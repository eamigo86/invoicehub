"""Complete customer addresses and replace the single email with a list.

The old email is copied into emails before the column is dropped. Customer is under forced
Row-Level Security, so the copy runs once per tenant with that tenant set, and the policy is
never lifted.
"""

import django.contrib.postgres.fields
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def _copy(schema_editor: BaseDatabaseSchemaEditor, statement: str) -> None:
    """Run a data statement against the customer rows of every tenant, one tenant at a time.

    Args:
        schema_editor: Editor whose connection runs the statements.
        statement: SQL that reads or updates the customer rows visible to the current tenant.
    """
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.tenant_id', true)")
        previous = cursor.fetchone()[0] or ""
        cursor.execute("SELECT id FROM tenancy_tenant")
        for (tenant_id,) in cursor.fetchall():
            cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_id)])
            cursor.execute(statement)
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [previous])


def copy_email_to_emails(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Copy every non-empty email into the emails list.

    Args:
        apps: Historical app registry, unused.
        schema_editor: Editor whose connection runs the statements.
    """
    _copy(
        schema_editor,
        "UPDATE billing_customer SET emails = ARRAY[email] WHERE email <> ''",
    )


def copy_emails_to_email(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Copy the first address of the emails list back into the email column.

    Args:
        apps: Historical app registry, unused.
        schema_editor: Editor whose connection runs the statements.
    """
    _copy(
        schema_editor,
        "UPDATE billing_customer SET email = COALESCE(emails[1], '')",
    )


class Migration(migrations.Migration):
    """Adds address and emails to the customer, migrating the old email value."""

    dependencies = [
        ("billing", "0002_enable_rls_customer"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="address_line1",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="customer",
            name="address_line2",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="customer",
            name="city",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="customer",
            name="country",
            field=models.CharField(default="US", max_length=2),
        ),
        migrations.AddField(
            model_name="customer",
            name="emails",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.EmailField(max_length=254), blank=True, default=list
            ),
        ),
        migrations.AddField(
            model_name="customer",
            name="postal_code",
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name="customer",
            name="state",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.RunPython(copy_email_to_emails, copy_emails_to_email),
        migrations.RemoveField(
            model_name="customer",
            name="email",
        ),
    ]
