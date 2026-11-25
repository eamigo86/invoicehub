"""Add the invoice PDF and let it be written once after issue.

The guard on issued invoices freezes every column except an allow-list. The pdf column joins
the allow-list, but only to go from empty to a value: once set it can neither be cleared nor
replaced, because the PDF of an issued invoice is the legal document and is never regenerated.
The function is replaced as a whole, as the migration that created it asked.
"""

import billing.models
from django.db import migrations, models

FORWARD = """
CREATE OR REPLACE FUNCTION billing_invoice_guard_update() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    allowed text[] := ARRAY['status', 'updated_at', 'pdf'];
BEGIN
    IF OLD.status <> 'draft' AND (
        NEW.status = 'draft'
        OR to_jsonb(OLD) - allowed IS DISTINCT FROM to_jsonb(NEW) - allowed
        OR (OLD.pdf <> '' AND NEW.pdf IS DISTINCT FROM OLD.pdf)
    ) THEN
        RAISE EXCEPTION 'issued invoices are immutable: invoice %', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$;
"""

REVERSE = """
CREATE OR REPLACE FUNCTION billing_invoice_guard_update() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    allowed text[] := ARRAY['status', 'updated_at'];
BEGIN
    IF OLD.status <> 'draft' AND (
        NEW.status = 'draft'
        OR to_jsonb(OLD) - allowed IS DISTINCT FROM to_jsonb(NEW) - allowed
    ) THEN
        RAISE EXCEPTION 'issued invoices are immutable: invoice %', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$;
"""


class Migration(migrations.Migration):
    """Adds the pdf column and replaces the update guard to allow writing it once."""

    dependencies = [("billing", "0018_enable_rls_payments")]

    operations = [
        migrations.AddField(
            model_name="invoice",
            name="pdf",
            field=models.FileField(
                blank=True, max_length=200, upload_to=billing.models.invoice_pdf_path
            ),
        ),
        migrations.RunSQL(FORWARD, REVERSE),
    ]
