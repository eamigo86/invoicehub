"""The mapping of domain exceptions to the typed errors of a mutation payload."""

import pytest
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from pydantic import BaseModel
from pydantic import ValidationError as InputValidationError

from api.errors import DomainError, ErrorCode, NotFound, domain_errors
from billing.services import InvalidTransition


def test_a_field_validation_error_gives_one_entry_per_field_and_message() -> None:
    """A message dict becomes entries with camelCase fields, and non-field errors have none."""
    exc = ValidationError({"tax_id": ["too long", "bad"], "__all__": ["whole object"]})
    assert domain_errors(exc) == [
        DomainError(ErrorCode.VALIDATION_ERROR, "too long", "taxId"),
        DomainError(ErrorCode.VALIDATION_ERROR, "bad", "taxId"),
        DomainError(ErrorCode.VALIDATION_ERROR, "whole object", None),
    ]


def test_a_validation_error_without_fields_gives_one_entry_without_a_field() -> None:
    """A plain message, or a list of messages, is a non-field error."""
    assert domain_errors(ValidationError(["one", "two"])) == [
        DomainError(ErrorCode.VALIDATION_ERROR, "one", None),
        DomainError(ErrorCode.VALIDATION_ERROR, "two", None),
    ]


def test_an_input_validation_error_gives_the_path_of_the_bad_value() -> None:
    """A pydantic error names the camelCase path, with list positions in brackets."""

    class Line(BaseModel):
        unit_price: int

    class Draft(BaseModel):
        lines: list[Line]

    with pytest.raises(InputValidationError) as caught:
        Draft.model_validate({"lines": [{"unit_price": 1}, {"unit_price": "x"}]})
    [entry] = domain_errors(caught.value)
    assert entry.code is ErrorCode.VALIDATION_ERROR
    assert entry.field == "lines[1].unitPrice"
    assert "integer" in entry.message


def test_the_other_domain_errors_map_to_their_codes() -> None:
    """Transitions, refusals and unknown rows have their own codes and no field."""
    assert domain_errors(InvalidTransition("invoice is issued")) == [
        DomainError(ErrorCode.INVALID_TRANSITION, "invoice is issued", None)
    ]
    assert domain_errors(PermissionDenied("missing permission invoices:write")) == [
        DomainError(ErrorCode.PERMISSION_DENIED, "missing permission invoices:write", None)
    ]
    assert domain_errors(NotFound("Customer not found.", "customerId")) == [
        DomainError(ErrorCode.NOT_FOUND, "Customer not found.", "customerId")
    ]
    assert domain_errors(ObjectDoesNotExist()) == [
        DomainError(ErrorCode.NOT_FOUND, "Not found.", None)
    ]


def test_an_unexpected_exception_is_not_swallowed() -> None:
    """Anything that is not a domain error propagates unchanged."""
    boom = RuntimeError("boom")
    with pytest.raises(RuntimeError) as caught:
        domain_errors(boom)
    assert caught.value is boom
