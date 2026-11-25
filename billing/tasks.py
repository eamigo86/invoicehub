"""Celery tasks of the billing app."""

from django.core.files.base import ContentFile
from weasyprint import HTML

from billing.models import Invoice
from billing.pdf import render_invoice_html
from billing.services import record_event
from config.celery import app
from tenancy.context import tenant_context


@app.task(name="billing.tasks.render_invoice_pdf", shared=False)  # type: ignore[untyped-decorator]
def render_invoice_pdf(tenant_id: str, invoice_id: str) -> None:
    """Render the PDF of an issued invoice once, as the system.

    The invoice id is the idempotency key. The row is locked, and a draft or an invoice that
    already has a PDF is left alone, so a second delivery never regenerates the legal
    document. Only the pdf field and updated_at are written, and invoice.pdf_rendered is
    recorded in the audit log. An invoice the tenant cannot see raises Invoice.DoesNotExist and
    nothing is written.

    Args:
        tenant_id: Tenant that owns the invoice.
        invoice_id: Invoice to render.
    """
    with tenant_context(tenant_id):
        invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
        if invoice.status == Invoice.Status.DRAFT or invoice.pdf:
            return
        content = HTML(string=render_invoice_html(invoice)).write_pdf()
        invoice.pdf.save(f"{invoice.pk}.pdf", ContentFile(content), save=False)
        invoice.save(update_fields=["pdf", "updated_at"])
        record_event(
            None, "invoice.pdf_rendered", "invoice", invoice.pk, after={"pdf": invoice.pdf.name}
        )
