"""Invoice calculations: half-up rounding, line amounts, totals and input validation."""

from collections.abc import Callable
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from billing.calculations import (
    InvoiceTotals,
    LineAmounts,
    LineInput,
    TaxRateInput,
    invoice_totals,
    line_amounts,
    round_half_up,
)

STATE = TaxRateInput("State", 600)
COUNTY = TaxRateInput("County", 225)


def line(
    quantity: str = "1",
    unit_price: int = 1000,
    discount_bp: int = 0,
    rates: tuple[TaxRateInput, ...] = (),
) -> LineInput:
    """Build a line input from readable values.

    Args:
        quantity: Quantity as a decimal string.
        unit_price: Unit price in cents.
        discount_bp: Discount in basis points.
        rates: Tax rates of the line.

    Returns:
        The line input.
    """
    return LineInput(Decimal(quantity), unit_price, discount_bp, rates)


@pytest.mark.parametrize(
    ("numerator", "denominator", "expected"),
    [(5, 2, 3), (7, 2, 4), (9, 2, 5), (4, 3, 1), (5, 3, 2), (1, 2, 1), (0, 7, 0), (6, 3, 2)],
)
def test_round_half_up(numerator: int, denominator: int, expected: int) -> None:
    """Exact halves round up where banker rounding would round 2.5 to 2 and 4.5 to 4.

    Args:
        numerator: Dividend.
        denominator: Divisor.
        expected: Expected rounded quotient.
    """
    assert round_half_up(numerator, denominator) == expected


def test_tax_of_8_25_percent_on_ten_dollars_is_83_cents() -> None:
    """A rate of 825 basis points on 10.00 gives 82.5 cents, rounded up to 83."""
    amounts = line_amounts(line(rates=(TaxRateInput("Sales", 825),)))
    assert amounts == LineAmounts(net=1000, taxes=(83,), total=1083)


def test_half_cent_net_rounds_up() -> None:
    """A net of 2.5 cents rounds to 3 where banker rounding gives 2."""
    assert line_amounts(line(quantity="0.5", unit_price=5)).net == 3


def test_fractional_quantity() -> None:
    """One and a half hours at 33.33 per hour is 49.995 and rounds to 50.00."""
    assert line_amounts(line(quantity="1.5", unit_price=3333)).net == 5000


def test_full_discount_gives_zero_amounts() -> None:
    """A 100 percent discount leaves a zero net, zero taxes and a zero total."""
    amounts = line_amounts(line(discount_bp=10000, rates=(STATE,)))
    assert amounts == LineAmounts(net=0, taxes=(0,), total=0)


def test_discount_is_applied_before_tax() -> None:
    """A 10 percent discount on 10.00 taxes the 9.00 net."""
    amounts = line_amounts(line(discount_bp=1000, rates=(STATE,)))
    assert amounts == LineAmounts(net=900, taxes=(54,), total=954)


def test_two_rates_on_one_line_are_taxed_on_the_net() -> None:
    """State and county tax each apply to the net and are rounded separately."""
    amounts = line_amounts(line(unit_price=1999, rates=(STATE, COUNTY)))
    assert amounts == LineAmounts(net=1999, taxes=(120, 45), total=2164)


def test_invoice_totals_sum_lines_and_break_down_by_rate() -> None:
    """Totals are sums of the lines and the breakdown adds up to the tax total."""
    totals = invoice_totals(
        [line(unit_price=1999, rates=(STATE, COUNTY)), line(unit_price=500, rates=(STATE,))]
    )
    assert totals.subtotal == 2499
    assert totals.tax_total == 120 + 45 + 30
    assert totals.total == totals.subtotal + totals.tax_total
    assert totals.breakdown == {STATE: 150, COUNTY: 45}
    assert [a.net for a in totals.lines] == [1999, 500]


def test_empty_invoice_has_zero_totals() -> None:
    """An invoice without lines totals zero."""
    assert invoice_totals([]) == InvoiceTotals((), 0, 0, 0, {})


@pytest.mark.parametrize(
    "bad",
    [
        lambda: line(quantity="1.0001"),
        lambda: line(quantity="-1"),
        lambda: line(unit_price=-1),
        lambda: line(discount_bp=10001),
        lambda: line(discount_bp=-1),
    ],
    ids=[
        "four-decimals",
        "negative-quantity",
        "negative-price",
        "discount-above",
        "discount-below",
    ],
)
def test_invalid_line_is_rejected(bad: Callable[[], LineInput]) -> None:
    """Out of range inputs raise ValueError when the line is built.

    Args:
        bad: Callable that builds an invalid line.
    """
    with pytest.raises(ValueError, match="."):
        bad()


quantities = st.integers(0, 1_000_000_000).map(lambda n: Decimal(n).scaleb(-3))
rate_inputs = st.builds(
    TaxRateInput,
    st.sampled_from(["State", "County", "City", "VAT"]),
    st.integers(0, 3000),
)
line_inputs = st.builds(
    LineInput,
    quantities,
    st.integers(0, 1_000_000_000),
    st.integers(0, 10000),
    st.lists(rate_inputs, max_size=3, unique=True).map(tuple),
)
invoices = st.lists(line_inputs, max_size=20)


def thousandths(quantity: Decimal) -> int:
    """Express a quantity as an integer count of thousandths.

    Args:
        quantity: Quantity with at most three decimal places.

    Returns:
        The number of thousandths.
    """
    return int(quantity.scaleb(3))


@given(invoices)
def test_invoice_totals_are_consistent(lines: list[LineInput]) -> None:
    """Totals equal the sums of the lines and the breakdown adds up exactly.

    Args:
        lines: Generated invoice lines.
    """
    totals = invoice_totals(lines)
    assert totals.total == totals.subtotal + totals.tax_total
    assert totals.subtotal == sum(a.net for a in totals.lines)
    assert totals.tax_total == sum(sum(a.taxes) for a in totals.lines)
    assert totals.total == sum(a.total for a in totals.lines)
    assert sum(totals.breakdown.values()) == totals.tax_total
    amounts = [totals.subtotal, totals.tax_total, totals.total, *totals.breakdown.values()]
    assert all(value >= 0 for value in amounts)


@given(line_inputs)
def test_line_amounts_are_exact_within_half_a_cent(item: LineInput) -> None:
    """Net and each tax are non-negative and within half a cent of the exact value.

    Args:
        item: Generated line.
    """
    amounts = line_amounts(item)
    assert len(amounts.taxes) == len(item.tax_rates)
    assert amounts.total == amounts.net + sum(amounts.taxes)
    exact_net = Decimal(thousandths(item.quantity) * item.unit_price * (10000 - item.discount_bp))
    exact_net /= Decimal(1000 * 10000)
    assert amounts.net >= 0
    assert abs(Decimal(amounts.net) - exact_net) <= Decimal("0.5")
    for tax, rate in zip(amounts.taxes, item.tax_rates, strict=True):
        exact_tax = Decimal(amounts.net * rate.rate_bp) / Decimal(10000)
        assert tax >= 0
        assert abs(Decimal(tax) - exact_tax) <= Decimal("0.5")


@given(line_inputs)
def test_discount_never_raises_the_net(item: LineInput) -> None:
    """The net never exceeds the undiscounted rounded gross, and no discount equals it.

    Args:
        item: Generated line.
    """
    gross = line_amounts(LineInput(item.quantity, item.unit_price, 0, item.tax_rates)).net
    assert line_amounts(item).net <= gross
    assert gross == round_half_up(thousandths(item.quantity) * item.unit_price, 1000)
