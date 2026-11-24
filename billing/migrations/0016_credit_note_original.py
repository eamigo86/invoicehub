"""Link a credit note to the invoice it corrects, and tie that link to the document kind."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Adds Invoice.original and the check constraint relating it to the kind."""

    dependencies = [
        ("billing", "0015_protect_line_links"),
        ("tenancy", "0004_tenant_issuer"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoice",
            name="original",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="credit_notes",
                to="billing.invoice",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("kind", "credit_note"), ("original__isnull", False)),
                    models.Q(("kind", "invoice"), ("original__isnull", True)),
                    _connector="OR",
                ),
                name="invoice_original_matches_kind",
            ),
        ),
    ]
