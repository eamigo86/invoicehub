"""Pure invoice arithmetic in integer cents and basis points, with no Django or database access."""

from dataclasses import dataclass
from decimal import Decimal

BASIS_POINTS = 10_000
THOUSANDTH = Decimal("0.001")


@dataclass(frozen=True)
class TaxRateInput:
    """A tax rate applied to a line.

    Attributes:
        name: Display name of the rate.
        rate_bp: Rate in basis points, where 825 means 8.25 percent.
    """

    name: str
    rate_bp: int


@dataclass(frozen=True)
class LineInput:
    """The inputs needed to price one invoice line.

    Attributes:
        quantity: Non-negative quantity with at most three decimal places.
        unit_price: Non-negative unit price in cents.
        discount_bp: Line discount in basis points, from 0 to 10000.
        tax_rates: Rates applied to the discounted net of the line.
    """

    quantity: Decimal
    unit_price: int
    discount_bp: int
    tax_rates: tuple[TaxRateInput, ...]

    def __post_init__(self) -> None:
        """Validate the ranges of the inputs.

        Raises:
            ValueError: If the quantity is negative or has more than three decimal places, the
                unit price is negative, or the discount is outside 0 to 10000 basis points.
        """
        if not self.quantity.is_finite() or self.quantity < 0:
            raise ValueError("quantity must be a non-negative number")
        if self.quantity != self.quantity.quantize(THOUSANDTH):
            raise ValueError("quantity allows at most 3 decimal places")
        if self.unit_price < 0:
            raise ValueError("unit price must not be negative")
        if not 0 <= self.discount_bp <= BASIS_POINTS:
            raise ValueError("discount must be between 0 and 10000 basis points")


@dataclass(frozen=True)
class LineAmounts:
    """The computed amounts of one invoice line, in cents.

    Attributes:
        net: Amount after discount and before tax.
        taxes: Tax per rate, aligned with the tax rates of the line.
        total: Net plus all taxes.
    """

    net: int
    taxes: tuple[int, ...]
    total: int


@dataclass(frozen=True)
class InvoiceTotals:
    """The computed totals of an invoice, in cents.

    Attributes:
        lines: Amounts of each line, in input order.
        subtotal: Sum of the line nets.
        tax_total: Sum of every line tax.
        total: Subtotal plus tax total.
        breakdown: Tax per rate, summing exactly to the tax total.
    """

    lines: tuple[LineAmounts, ...]
    subtotal: int
    tax_total: int
    total: int
    breakdown: dict[TaxRateInput, int]


def round_half_up(numerator: int, denominator: int) -> int:
    """Divide two non-negative integers and round half up.

    Args:
        numerator: Non-negative dividend.
        denominator: Positive divisor.

    Returns:
        The quotient rounded to the nearest integer, with exact halves rounded up.
    """
    return (2 * numerator + denominator) // (2 * denominator)


def line_amounts(line: LineInput) -> LineAmounts:
    """Compute the net, taxes and total of one line.

    Args:
        line: The validated line inputs.

    Returns:
        The amounts of the line in cents.
    """
    thousandths = int(line.quantity.scaleb(3))
    net = round_half_up(
        thousandths * line.unit_price * (BASIS_POINTS - line.discount_bp), 1000 * BASIS_POINTS
    )
    taxes = tuple(round_half_up(net * rate.rate_bp, BASIS_POINTS) for rate in line.tax_rates)
    return LineAmounts(net, taxes, net + sum(taxes))


def invoice_totals(lines: list[LineInput]) -> InvoiceTotals:
    """Compute the amounts of every line and the invoice totals.

    Args:
        lines: The validated line inputs.

    Returns:
        The line amounts, totals and the per-rate tax breakdown.
    """
    amounts = tuple(line_amounts(line) for line in lines)
    breakdown: dict[TaxRateInput, int] = {}
    for line, item in zip(lines, amounts, strict=True):
        for rate, tax in zip(line.tax_rates, item.taxes, strict=True):
            breakdown[rate] = breakdown.get(rate, 0) + tax
    subtotal = sum(item.net for item in amounts)
    tax_total = sum(breakdown.values())
    return InvoiceTotals(amounts, subtotal, tax_total, subtotal + tax_total, breakdown)
