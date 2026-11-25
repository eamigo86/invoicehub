"""Pure bank statement parsing: CSV text to signed integer cents with a dedupe hash, no database."""

import csv
import hashlib
import io
import json
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

DESCRIPTION_MAX_LENGTH = 500
"""Longest description a BankTransaction stores."""
PAYER_MAX_LENGTH = 200
"""Longest payer a BankTransaction stores."""

# Plain digits, or digits grouped by thousands commas, with 0 to 2 decimal places.
AMOUNT_RE = re.compile(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?")


class StatementError(ValueError):
    """An unusable statement file, with a message naming the data row and the problem."""


@dataclass(frozen=True)
class ParsedRow:
    """One statement row ready to be stored.

    Attributes:
        value_date: Date the bank booked the transaction.
        amount: Signed amount in cents, negative for money out.
        description: Bank description of the transaction.
        payer: Payer name, empty when the bank gives none.
        dedupe_hash: SHA-256 hex digest that identifies the row across overlapping statements.
    """

    value_date: date
    amount: int
    description: str
    payer: str
    dedupe_hash: str


def to_cents(text: str) -> int:
    """Parse a North American bank amount into signed integer cents.

    Accepts surrounding whitespace, a dollar sign, thousands commas, 0 to 2 decimal places, a
    leading minus and accounting parentheses for negatives.

    Args:
        text: Amount as printed by the bank.

    Returns:
        The signed amount in cents.

    Raises:
        ValueError: If the text is empty, has more than 2 decimal places, has a malformed
            grouping or is not a number.
    """
    body = text.strip()
    negative = False
    if body.startswith("(") and body.endswith(")"):
        negative, body = True, body[1:-1].strip()
    elif body.startswith("-"):
        negative, body = True, body[1:].strip()
    body = body.removeprefix("$")
    if not AMOUNT_RE.fullmatch(body):
        raise ValueError(f"not a valid amount: {text!r}")
    cents = int(Decimal(body.replace(",", "")).scaleb(2))
    return -cents if negative else cents


def parse_statement(content: bytes, mapping: Mapping[str, str], account: str) -> list[ParsedRow]:
    """Parse a bank statement CSV, failing the whole file on the first bad row.

    Blank lines and zero amount rows are skipped. Row numbers in errors are 1-based over the data
    rows, so skipped rows still count.

    Args:
        content: Raw bytes of the file, UTF-8 with an optional byte order mark.
        mapping: Import snapshot with date_column, amount_column, description_column,
            payer_column (empty for no payer) and date_format.
        account: Stable identifier of the bank account, mixed into every hash.

    Returns:
        The parsed rows in file order.

    Raises:
        StatementError: If the bytes are not UTF-8, a mapped column is missing, or a row has a
            bad date or amount, or a description or payer is too long for its column.
    """
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise StatementError("the file is not valid UTF-8 text") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    columns = {key: mapping[key] for key in ("date_column", "amount_column", "description_column")}
    payer_column = mapping["payer_column"]
    wanted = [*columns.values(), *([payer_column] if payer_column else [])]
    header = reader.fieldnames or []
    for name in wanted:
        if name not in header:
            raise StatementError(f"column {name!r} is missing from the header")

    def cell(row: Mapping[str, str | None], name: str) -> str:
        return (row.get(name) or "").strip()

    seen: Counter[tuple[str, int, str]] = Counter()
    rows: list[ParsedRow] = []
    for number, row in enumerate(reader, start=1):
        values = {name: cell(row, name) for name in wanted}
        if not any(values.values()):
            continue
        try:
            value_date = datetime.strptime(
                values[columns["date_column"]], mapping["date_format"]
            ).date()
        except ValueError as exc:
            raise StatementError(f"row {number}: bad date: {exc}") from exc
        try:
            amount = to_cents(values[columns["amount_column"]])
        except ValueError as exc:
            raise StatementError(f"row {number}: {exc}") from exc
        if amount == 0:
            continue
        description = values[columns["description_column"]]
        payer = values[payer_column] if payer_column else ""
        if len(description) > DESCRIPTION_MAX_LENGTH:
            raise StatementError(
                f"row {number}: description is longer than {DESCRIPTION_MAX_LENGTH} characters"
            )
        if len(payer) > PAYER_MAX_LENGTH:
            raise StatementError(
                f"row {number}: payer is longer than {PAYER_MAX_LENGTH} characters"
            )
        # ponytail: occurrence counts repeats within one file, so two statements that split one
        # day's identical rows differently can collide or duplicate. Upgrade path: hash a
        # bank-provided transaction id when the CSV has one.
        key = (value_date.isoformat(), amount, description)
        occurrence = seen[key]
        seen[key] += 1
        # JSON of a list keeps field boundaries unambiguous, whatever the description contains.
        encoded = json.dumps([*key, account, occurrence], ensure_ascii=True)
        rows.append(
            ParsedRow(
                value_date=value_date,
                amount=amount,
                description=description,
                payer=payer,
                dedupe_hash=hashlib.sha256(encoded.encode()).hexdigest(),
            )
        )
    return rows
