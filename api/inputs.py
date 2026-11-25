"""Pydantic models for the arguments of the mutations.

Each model is compiled by django-graphex into a GraphQL input type, and the mutations validate
their arguments against it, so a bad value comes back as a typed error in the payload instead of
a GraphQL error.
"""

import datetime
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Any

from django_graphex.core import InputType
from django_graphex.core.fields import BigIntValue, IDScalar
from pydantic import BaseModel, Field, field_validator

from core.models import Currency

if TYPE_CHECKING:
    RowId = int
    InvoiceId = uuid.UUID
    Cents = int
else:
    # The ID scalar accepts the string a query returns; the validators below convert it.
    RowId = InvoiceId = IDScalar
    Cents = BigIntValue


def _row_id(cls: Any, value: Any) -> Any:
    """Convert an id received as a string, or a list of them, to integers.

    Args:
        cls: Model class, supplied by pydantic.
        value: Id, list of ids, or None.

    Returns:
        The same shape with integers.

    Raises:
        ValueError: When an id is not an integer.
    """
    if isinstance(value, list):
        return [_row_id(cls, item) for item in value]
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        raise ValueError("Not a valid id.") from None


def _invoice_id(cls: Any, value: Any) -> Any:
    """Convert an invoice id received as a string to a UUID.

    Args:
        cls: Model class, supplied by pydantic.
        value: Id or None.

    Returns:
        The UUID, or None.

    Raises:
        ValueError: When the id is not a UUID.
    """
    try:
        return None if value is None else uuid.UUID(str(value))
    except ValueError:
        raise ValueError("Not a valid id.") from None


class CustomerInput(InputType):
    """The fields of a customer; with an id they replace those of the customer."""

    id: RowId | None = None
    name: str
    tax_id: str = ""
    emails: list[str] = []
    address_line1: str = ""
    address_line2: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = "US"

    _check_id = field_validator("id")(_row_id)


class DraftLineInput(InputType):
    """One line of a draft; money is in integer cents and the quantity has up to 3 decimals."""

    description: Annotated[str, Field(max_length=500)]
    quantity: Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=3)]
    unit_price: Annotated[Cents, Field(ge=0)]
    discount_bp: Annotated[int, Field(ge=0, le=10000)] = 0
    product_id: RowId | None = None
    tax_rate_ids: list[RowId] = []

    _check_ids = field_validator("product_id", "tax_rate_ids")(_row_id)


class DraftInvoiceInput(InputType):
    """A draft invoice and its lines; with an id they replace those of the draft.

    Omitting the currency keeps the current one, or uses the default of the tenant for a new
    draft.
    """

    id: InvoiceId | None = None
    customer_id: RowId
    currency: Currency | None = None
    due_date: datetime.date | None = None
    notes: str = ""
    lines: list[DraftLineInput]

    _check_id = field_validator("id")(_invoice_id)
    _check_customer = field_validator("customer_id")(_row_id)


class DraftReference(BaseModel):
    """The id argument of deleteDraft, which is validated like the id of a draft input."""

    id: uuid.UUID
