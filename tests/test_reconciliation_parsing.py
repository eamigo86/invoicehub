"""Bank statement parsing: amounts in cents, CSV rows, row-numbered errors and dedupe hashes."""

from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from reconciliation.parsing import ParsedRow, StatementError, parse_statement, to_cents

MAPPING = {
    "date_column": "Date",
    "amount_column": "Amount",
    "description_column": "Description",
    "payer_column": "Payer",
    "date_format": "%m/%d/%Y",
}
HEADER = "Date,Amount,Description,Payer\n"


def parse(body: str, mapping: dict[str, str] = MAPPING, account: str = "acct-1") -> list[ParsedRow]:
    """Parse a CSV body that follows the standard header.

    Args:
        body: Data lines of the CSV, without the header.
        mapping: Column mapping to use.
        account: Stable account identifier.

    Returns:
        The parsed rows.
    """
    return parse_statement((HEADER + body).encode(), mapping, account)


@pytest.mark.parametrize(
    ("text", "cents"),
    [
        ("12", 1200),
        ("12.5", 1250),
        ("12.50", 1250),
        ("0.05", 5),
        ("$12.50", 1250),
        ("  1,234.56  ", 123456),
        ("$1,234,567.89", 123456789),
        ("-12.50", -1250),
        ("-$1,234.56", -123456),
        ("(1,234.56)", -123456),
        ("($12.50)", -1250),
        ("0", 0),
    ],
)
def test_to_cents_accepts(text: str, cents: int) -> None:
    """Accepted amount forms become signed integer cents.

    Args:
        text: Amount as printed by the bank.
        cents: Expected signed cents.
    """
    assert to_cents(text) == cents


@pytest.mark.parametrize(
    "text",
    ["", "   ", "12.345", "abc", "1,23.4", "1,2345.00", "12,", "--5", "(5", "$", "1.2.3", "nan"],
)
def test_to_cents_refuses(text: str) -> None:
    """Empty, over-precise, non-numeric and badly grouped amounts are refused.

    Args:
        text: Unusable amount text.
    """
    with pytest.raises(ValueError):
        to_cents(text)


def test_parse_maps_columns() -> None:
    """A row is mapped to a date, cents, description and payer."""
    (row,) = parse('01/31/2026,"1,250.00",Invoice 7,Acme Inc\n')
    assert (row.value_date, row.amount, row.description, row.payer) == (
        date(2026, 1, 31),
        125000,
        "Invoice 7",
        "Acme Inc",
    )
    assert len(row.dedupe_hash) == 64


def test_parse_strips_values() -> None:
    """Whitespace around values is stripped."""
    (row,) = parse(" 01/31/2026 , 10.00 ,  Rent  ,  Bob \n")
    assert (row.description, row.payer, row.amount) == ("Rent", "Bob", 1000)


def test_parse_accepts_bom() -> None:
    """A UTF-8 byte order mark does not corrupt the first header."""
    content = ("﻿" + HEADER + "01/31/2026,10.00,Rent,Bob\n").encode()
    assert len(parse_statement(content, MAPPING, "a")) == 1


def test_parse_missing_column_names_it() -> None:
    """A mapped column absent from the header is reported by name."""
    with pytest.raises(StatementError, match="Amount"):
        parse_statement(b"Date,Description,Payer\n01/31/2026,Rent,Bob\n", MAPPING, "a")


def test_parse_undecodable_bytes() -> None:
    """Bytes that are not UTF-8 fail the whole file."""
    with pytest.raises(StatementError, match="UTF-8"):
        parse_statement(b"\xff\xfe\x00bad", MAPPING, "a")


def test_parse_bad_date_names_row() -> None:
    """A bad date names its 1-based data row."""
    with pytest.raises(StatementError, match="row 2"):
        parse("01/31/2026,10.00,A,B\n31/31/2026,10.00,C,D\n")


def test_parse_bad_amount_names_row() -> None:
    """A bad amount names its 1-based data row."""
    with pytest.raises(StatementError, match="row 3"):
        parse("01/31/2026,10.00,A,B\n01/31/2026,5.00,A,B\n01/31/2026,1.234,C,D\n")


def test_parse_skips_blank_and_zero_rows() -> None:
    """Blank lines and zero amounts are skipped without breaking row numbers."""
    rows = parse("\n01/31/2026,0.00,Fee waived,\n,,,\n01/31/2026,10.00,Rent,Bob\n")
    assert [r.description for r in rows] == ["Rent"]


def test_parse_row_number_counts_skipped_rows() -> None:
    """Row numbers follow the file, so skipped rows still count."""
    with pytest.raises(StatementError, match="row 3"):
        parse("01/31/2026,0.00,A,B\n01/31/2026,10.00,A,B\nbad,10.00,C,D\n")


def test_parse_without_payer_column() -> None:
    """An empty payer column mapping yields an empty payer."""
    mapping = {**MAPPING, "payer_column": ""}
    (row,) = parse_statement(b"Date,Amount,Description\n01/31/2026,10.00,Rent\n", mapping, "a")
    assert row.payer == ""


def test_parse_fails_whole_file_on_first_bad_row() -> None:
    """No rows are returned when any row is bad."""
    with pytest.raises(StatementError):
        parse("01/31/2026,10.00,A,B\nbad,10.00,C,D\n")


def test_identical_rows_get_different_hashes() -> None:
    """Two identical payments on one day both survive."""
    a, b = parse("01/31/2026,50.00,Coffee,Bob\n01/31/2026,50.00,Coffee,Bob\n")
    assert a.dedupe_hash != b.dedupe_hash


def test_same_content_same_hashes() -> None:
    """Re-parsing the same file yields the same hashes."""
    body = "01/31/2026,50.00,Coffee,Bob\n01/31/2026,50.00,Coffee,Bob\n02/01/2026,5.00,Tea,\n"
    assert [r.dedupe_hash for r in parse(body)] == [r.dedupe_hash for r in parse(body)]


def test_overlapping_statements_share_hashes() -> None:
    """A full day present in two statements dedupes to the same hashes."""
    day = "01/31/2026,50.00,Coffee,Bob\n01/31/2026,50.00,Coffee,Bob\n"
    first = parse("01/30/2026,1.00,X,\n" + day)
    second = parse(day + "02/01/2026,2.00,Y,\n")
    assert {r.dedupe_hash for r in first} & {r.dedupe_hash for r in second} == {
        r.dedupe_hash for r in parse(day)
    }


def test_different_account_different_hashes() -> None:
    """The account takes part in the hash."""
    body = "01/31/2026,50.00,Coffee,Bob\n"
    assert parse(body, account="a")[0].dedupe_hash != parse(body, account="b")[0].dedupe_hash


def test_hash_encoding_is_unambiguous() -> None:
    """Field boundaries cannot be shifted between description and account."""
    one = parse("01/31/2026,5.00,x|y,\n", account="z")[0]
    two = parse("01/31/2026,5.00,x,\n", account="y|z")[0]
    assert one.dedupe_hash != two.dedupe_hash


def format_cents(cents: int, style: str, dollar: bool, commas: bool) -> str:
    """Render cents in one accepted bank style.

    Args:
        cents: Signed cents.
        style: Either minus or parens for negatives.
        dollar: Whether to prefix a dollar sign.
        commas: Whether to group thousands with commas.

    Returns:
        The formatted amount.
    """
    magnitude = abs(cents)
    body = f"{magnitude // 100:,}" if commas else str(magnitude // 100)
    body = f"{'$' if dollar else ''}{body}.{magnitude % 100:02d}"
    if cents >= 0:
        return body
    return f"({body})" if style == "parens" else f"-{body}"


@given(
    st.integers(min_value=-(10**12), max_value=10**12),
    st.sampled_from(["minus", "parens"]),
    st.booleans(),
    st.booleans(),
)
def test_to_cents_round_trips(cents: int, style: str, dollar: bool, commas: bool) -> None:
    """Any cents value round-trips through every accepted style.

    Args:
        cents: Generated signed cents.
        style: Negative style.
        dollar: Whether a dollar sign is used.
        commas: Whether thousands are grouped.
    """
    assert to_cents(format_cents(cents, style, dollar, commas)) == cents


@given(
    st.lists(
        st.tuples(
            st.dates(min_value=date(2000, 1, 1), max_value=date(2100, 1, 1)),
            st.integers(min_value=1, max_value=10**9),
            st.text(alphabet='ab ,|"', max_size=4).filter(lambda s: s == s.strip()),
        ),
        max_size=30,
    )
)
def test_hashes_are_unique_within_a_file(rows: list[tuple[date, int, str]]) -> None:
    """Every parsed row of any generated file has its own hash.

    Args:
        rows: Generated date, cents and description triples, repeats allowed.
    """
    import csv
    import io

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Date", "Amount", "Description", "Payer"])
    for d, cents, desc in rows:
        writer.writerow(
            [d.strftime("%m/%d/%Y"), format_cents(cents, "minus", False, False), desc, ""]
        )
    parsed = parse_statement(out.getvalue().encode(), MAPPING, "a")
    assert len(parsed) == len(rows)
    assert len({r.dedupe_hash for r in parsed}) == len(parsed)
