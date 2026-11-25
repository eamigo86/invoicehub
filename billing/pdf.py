"""Rendering of issued invoices as HTML and PDF."""

from collections import defaultdict
from typing import Any

from django.template.loader import render_to_string

from billing.models import Invoice, InvoiceLineTax


def format_money(cents: int, currency: str) -> str:
    """Format an amount in cents with integer arithmetic only.

    Args:
        cents: Amount in cents, negative for a credit.
        currency: Currency code shown before the amount.

    Returns:
        The amount such as USD 1,234.56, with a leading minus sign for a negative amount.
    """
    units, rest = divmod(abs(cents), 100)
    sign = "-" if cents < 0 else ""
    return f"{sign}{currency} {units:,}.{rest:02d}"


def format_basis_points(basis_points: int) -> str:
    """Format basis points as a percentage with integer arithmetic only.

    Args:
        basis_points: Rate in basis points, where 825 means 8.25 percent.

    Returns:
        The rate such as 8.25%.
    """
    return f"{basis_points // 100}.{basis_points % 100:02d}%"


def _tax_breakdown(invoice: Invoice, currency: str) -> list[dict[str, str]]:
    """Group the line taxes by name and rate, adding up to the tax total of the invoice.

    Args:
        invoice: Issued invoice.
        currency: Currency code of the invoice.

    Returns:
        One row per name and rate, in the order the taxes first appear, with the formatted
        basis and amount.
    """
    groups: dict[tuple[str, int], list[int]] = defaultdict(lambda: [0, 0])
    taxes = InvoiceLineTax.objects.filter(line__invoice=invoice).select_related("line")
    for tax in taxes.order_by("line__position", "pk"):
        group = groups[(tax.name, tax.rate_bp)]
        group[0] += tax.line.net
        group[1] += tax.amount
    return [
        {
            "label": f"{name} ({format_basis_points(rate_bp)})",
            "basis": format_money(basis, currency),
            "amount": format_money(amount, currency),
        }
        for (name, rate_bp), (basis, amount) in groups.items()
    ]


def render_invoice_html(invoice: Invoice) -> str:
    """Render the HTML of an issued invoice or credit note.

    Args:
        invoice: Issued invoice of the current tenant.

    Returns:
        The HTML document, styled with plain CSS because WeasyPrint has no Tailwind.
    """
    currency = invoice.currency
    lines: list[dict[str, Any]] = [
        {
            "description": line.description,
            "quantity": format(line.quantity.normalize(), "f"),
            "unit_price": format_money(line.unit_price, currency),
            "discount": format_basis_points(line.discount_bp) if line.discount_bp else "",
            "net": format_money(line.net, currency),
        }
        for line in invoice.lines.order_by("position")
    ]
    context = {
        "invoice": invoice,
        "is_credit_note": invoice.kind == Invoice.Kind.CREDIT_NOTE,
        "original_reference": invoice.original.reference if invoice.original else "",
        "lines": lines,
        "breakdown": _tax_breakdown(invoice, currency),
        "subtotal": format_money(invoice.subtotal, currency),
        "tax_total": format_money(invoice.tax_total, currency),
        "total": format_money(invoice.total, currency),
    }
    return render_to_string("billing/invoice_pdf.html", context)
