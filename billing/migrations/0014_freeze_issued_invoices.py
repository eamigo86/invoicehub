"""Freeze issued invoices, their lines and their line taxes with database triggers.

This is the backstop behind the services: the admin, bulk updates and raw SQL cannot get around
it. The functions run with the privileges of the caller, so Row-Level Security applies to their
lookups and a parent invoice that cannot be seen counts as not being a draft: the checks fail
closed. The draft checks lock the parent invoice row FOR SHARE, so a line written while another
transaction issues that invoice waits for it and then sees the new status instead of slipping in.
Every refusal raises SQLSTATE 23000, which Django surfaces as IntegrityError.

An invoice that is not a draft accepts an UPDATE only when the rows before and after, compared
as jsonb, differ solely in the keys of the allow-list below (status and updated_at), and the
status does not go back to draft. Every other column is frozen by default, new columns
included. A later migration that adds a field legitimately written after issue (PDF, sending,
payments) must replace billing_invoice_guard_update with an extended allow-list.
"""

from django.db import migrations

FORWARD = """
CREATE FUNCTION billing_invoice_is_draft(invoice uuid) RETURNS boolean
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM 1 FROM billing_invoice WHERE id = invoice AND status = 'draft' FOR SHARE;
    RETURN FOUND;
END
$$;

CREATE FUNCTION billing_line_invoice_is_draft(line bigint) RETURNS boolean
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM 1
    FROM billing_invoiceline AS l
    JOIN billing_invoice AS i ON i.id = l.invoice_id
    WHERE l.id = line AND i.status = 'draft'
    FOR SHARE OF i;
    RETURN FOUND;
END
$$;

CREATE FUNCTION billing_invoice_guard_update() RETURNS trigger
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

CREATE FUNCTION billing_invoice_guard_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'draft' THEN
        RAISE EXCEPTION 'issued invoices are immutable: invoice %', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN OLD;
END
$$;

CREATE FUNCTION billing_invoiceline_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') AND NOT billing_invoice_is_draft(OLD.invoice_id)
        OR TG_OP IN ('INSERT', 'UPDATE') AND NOT billing_invoice_is_draft(NEW.invoice_id)
    THEN
        RAISE EXCEPTION 'issued invoices are immutable: their lines cannot change'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END
$$;

CREATE FUNCTION billing_invoicelinetax_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') AND NOT billing_line_invoice_is_draft(OLD.line_id)
        OR TG_OP IN ('INSERT', 'UPDATE') AND NOT billing_line_invoice_is_draft(NEW.line_id)
    THEN
        RAISE EXCEPTION 'issued invoices are immutable: their taxes cannot change'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END
$$;

CREATE TRIGGER billing_invoice_guard_update BEFORE UPDATE ON billing_invoice
    FOR EACH ROW EXECUTE FUNCTION billing_invoice_guard_update();
CREATE TRIGGER billing_invoice_guard_delete BEFORE DELETE ON billing_invoice
    FOR EACH ROW EXECUTE FUNCTION billing_invoice_guard_delete();
CREATE TRIGGER billing_invoiceline_guard BEFORE INSERT OR UPDATE OR DELETE ON billing_invoiceline
    FOR EACH ROW EXECUTE FUNCTION billing_invoiceline_guard();
CREATE TRIGGER billing_invoicelinetax_guard
    BEFORE INSERT OR UPDATE OR DELETE ON billing_invoicelinetax
    FOR EACH ROW EXECUTE FUNCTION billing_invoicelinetax_guard();
"""

REVERSE = """
DROP TRIGGER billing_invoicelinetax_guard ON billing_invoicelinetax;
DROP TRIGGER billing_invoiceline_guard ON billing_invoiceline;
DROP TRIGGER billing_invoice_guard_delete ON billing_invoice;
DROP TRIGGER billing_invoice_guard_update ON billing_invoice;
DROP FUNCTION billing_invoicelinetax_guard();
DROP FUNCTION billing_invoiceline_guard();
DROP FUNCTION billing_invoice_guard_delete();
DROP FUNCTION billing_invoice_guard_update();
DROP FUNCTION billing_line_invoice_is_draft(bigint);
DROP FUNCTION billing_invoice_is_draft(uuid);
"""


class Migration(migrations.Migration):
    """Creates the guard functions and triggers on the invoice tables."""

    dependencies = [("billing", "0013_enable_rls_invoiceseries")]

    operations = [migrations.RunSQL(FORWARD, REVERSE)]
