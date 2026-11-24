# RFC 0003 — Billing domain

**Status:** draft

An issued invoice is immutable: it is numbered consecutively with no gaps within its series, its amounts are frozen, and any correction is made with a credit note.

## Data model

| Entity | Key fields | Notes |
| --- | --- | --- |
| `Tenant` | legal name, EIN, address, default currency (USD) and timezone | Issuer data copied to each invoice |
| `Customer` | name, optional tax ID (e.g. EIN), emails, address | Copied to the invoice when issued (snapshot) |
| `Product` | name, unit price in cents, default tax | Optional catalog; a line can be free-form |
| `TaxRate` | name, percentage in basis points | Configurable per tenant (e.g. a state or local sales tax) |
| `InvoiceSeries` | prefix, next number, reset year | One per type: regular, credit note |
| `Invoice` | series, number, status, dates, currency, totals, original invoice (if credit note) | Totals calculated and stored |
| `InvoiceLine` | description, quantity, unit price, discount, taxes | Per-line rounding, documented |
| `Payment` | amount, date, source (manual or bank), invoices covered | Created by reconciliation (RFC 0005) |
| `AuditEvent` | actor (user, token or AI), action, before/after | Every action on an invoice |

## Lifecycle

1. **Draft:** editable, no number.
2. **Issued:** a number is assigned, data is frozen and the PDF is generated. It is no longer edited.
3. **Sent:** email with the PDF, recorded in the audit log.
4. **Partially paid / Paid:** according to the payments applied.
5. **Voided:** only through a credit note that references it; overdue is a status derived from the date, not stored.

## Gap-free numbering

The number is assigned within the same transaction that issues the invoice, locking the series row:

```python
from django.db import transaction


class InvoiceIssuer:
    """Issue draft invoices with gap-free numbering per series."""

    @transaction.atomic
    def issue(self, invoice: "Invoice") -> "Invoice":
        """Assign the next number, freeze snapshots and mark the invoice as issued.

        Locks the series row with SELECT ... FOR UPDATE so concurrent issues
        in the same series are serialized and no number is skipped or reused.

        Args:
            invoice: A draft invoice belonging to the current tenant.

        Returns:
            The issued invoice.

        Raises:
            InvalidTransition: If the invoice is not a draft.
        """
        series = InvoiceSeries.objects.select_for_update().get(pk=invoice.series_id)
        invoice.number = series.next_number
        series.next_number += 1
        series.save(update_fields=["next_number"])
        invoice.freeze_snapshots()
        invoice.status = Invoice.Status.ISSUED
        invoice.save()
        transaction.on_commit(lambda: render_invoice_pdf.delay(invoice.tenant_id, invoice.pk))
        return invoice
```

## Taxes and rounding

- Amounts in cents; percentages in basis points (8.25 % = 825).
- The net of a line is quantity times unit price minus the line discount, rounded half up to the cent. Each tax rate of the line is applied to that net and rounded half up to the cent, so every rate is rounded on its own.
- A line total is its net plus its taxes. The invoice subtotal is the sum of the line nets, the tax total is the sum of every line tax, and the invoice total is the subtotal plus the tax total.
- The per-rate breakdown sums the line taxes of each rate, so it always adds up exactly to the tax total. Hypothesis property tests check these identities, and CI runs them derandomized.
- Multiple rates per line (e.g. state + county sales tax) with a per-rate breakdown in the PDF.
- Rates are configured by the tenant: InvoiceHub does not decide which rates apply (nexus, exemptions, product taxability) and does not promise tax compliance.

## PDFs and files

- WeasyPrint with HTML + CSS templates, generated in a Celery task after issuing.
- Stored in GCS at path `tenants/<tenant_id>/invoices/<id>.pdf`, served with short-lived signed URLs.
- The PDF of an issued invoice is not regenerated: it is the legal document.

## Alternatives considered

| Alternative | Why it was rejected |
| --- | --- |
| Postgres sequences for numbering | They leave gaps when a transaction rolls back |
| Decimal instead of cents | Valid, but integers simplify integration with remitmatch and the API |
| Generating the PDF in the request | Blocks the user for seconds; better asynchronous |
