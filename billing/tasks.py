"""Celery tasks of the billing app."""

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.mail import EmailMessage
from django.utils import timezone
from weasyprint import HTML

from billing.models import Invoice, InvoiceEmail
from billing.pdf import format_money, render_invoice_html
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


@app.task(name="billing.tasks.send_invoice_email", shared=False)  # type: ignore[untyped-decorator]
def send_invoice_email(tenant_id: str, email_id: str) -> None:
    """Deliver an invoice email request once, as the system.

    The request row is locked and one with sent_at set is left alone, so a second delivery of
    the message never mails again. The PDF is rendered on the spot when it is missing. One
    email with the PDF attached goes from DEFAULT_FROM_EMAIL to the recipients, sent_at is set,
    an issued invoice becomes sent while any other status is kept, and invoice.sent is recorded
    in the audit log. A request the tenant cannot see raises InvoiceEmail.DoesNotExist and
    nothing is sent.

    Args:
        tenant_id: Tenant that owns the request.
        email_id: Request to deliver.
    """
    # ponytail: delivery is at least once. If the mail goes out and the commit then fails, a retry
    # sends again; exactly once would need the mail provider's idempotency key.
    with tenant_context(tenant_id):
        request = InvoiceEmail.objects.select_for_update().get(pk=email_id)
        if request.sent_at is not None:
            return
        render_invoice_pdf(tenant_id, str(request.invoice_id))
        invoice = Invoice.objects.get(pk=request.invoice_id)
        issuer = (invoice.issuer or {}).get("legal_name", "")
        total = format_money(invoice.total, invoice.currency)
        message = EmailMessage(
            subject=f"Invoice {invoice.reference} from {issuer}",
            body=f"{issuer} sent you invoice {invoice.reference} for {total}. It is attached.",
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=request.recipients,
        )
        with invoice.pdf.open("rb") as pdf:
            message.attach(f"{invoice.reference}.pdf", pdf.read(), "application/pdf")
        message.send()
        request.sent_at = timezone.now()
        request.save(update_fields=["sent_at"])
        if invoice.status == Invoice.Status.ISSUED:
            invoice.status = Invoice.Status.SENT
            invoice.save(update_fields=["status", "updated_at"])
        record_event(
            None, "invoice.sent", "invoice", invoice.pk, after={"recipients": request.recipients}
        )
