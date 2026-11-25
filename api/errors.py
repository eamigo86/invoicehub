"""Typed domain errors for mutation payloads, and the one mapping from exceptions to them."""

import enum
from dataclasses import dataclass

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from graphql import (
    GraphQLEnumType,
    GraphQLField,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLString,
)
from pydantic import ValidationError as InputValidationError
from pydantic.alias_generators import to_camel

from billing.services import InvalidTransition


class ErrorCode(enum.StrEnum):
    """The kinds of failure a mutation reports in its payload."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_FOUND = "NOT_FOUND"


@dataclass(frozen=True)
class DomainError:
    """One failure of a mutation.

    Attributes:
        code: What kind of failure it is.
        message: Human readable explanation.
        field: GraphQL path of the input value at fault, such as lines[0].unitPrice, or None
            when the failure is not about one value.
    """

    code: ErrorCode
    message: str
    field: str | None = None


class NotFound(ObjectDoesNotExist):
    """Raised when an id given by the caller names no row the caller may read.

    A row of another tenant and a row that does not exist look the same on purpose.
    """

    def __init__(self, message: str, field: str | None = None) -> None:
        """Keep the message and the path of the offending id.

        Args:
            message: Explanation for the caller.
            field: GraphQL path of the id that was not found, if any.
        """
        super().__init__(message)
        self.field = field


HANDLED = (
    ValidationError,
    InputValidationError,
    InvalidTransition,
    PermissionDenied,
    ObjectDoesNotExist,
)
"""The exception types that become payload errors; anything else is a bug and propagates."""


def _path(location: tuple[int | str, ...]) -> str | None:
    """Render a pydantic error location as a GraphQL input path.

    Args:
        location: Keys and list positions leading to the bad value.

    Returns:
        A path such as lines[0].unitPrice, or None for an error about the whole input.
    """
    path = ""
    for part in location:
        path += f"[{part}]" if isinstance(part, int) else f".{to_camel(part)}"
    return path.removeprefix(".") or None


def domain_errors(exc: Exception) -> list[DomainError]:
    """Turn an exception raised by a service or an input check into payload errors.

    An exception that is not a domain error is raised again unchanged.

    Args:
        exc: Exception to describe, one of HANDLED.

    Returns:
        One entry per field and message for validation errors, otherwise a single entry.
    """
    code = ErrorCode.VALIDATION_ERROR
    if isinstance(exc, InputValidationError):
        return [
            DomainError(code, error["msg"], _path(error["loc"]))
            for error in exc.errors(include_url=False)
        ]
    if isinstance(exc, ValidationError):
        fields: dict[str | None, list[str]] = (
            {
                None if name == "__all__" else to_camel(name): msgs
                for name, msgs in exc.message_dict.items()
            }
            if hasattr(exc, "error_dict")
            else {None: exc.messages}
        )
        return [DomainError(code, text, name) for name, texts in fields.items() for text in texts]
    if isinstance(exc, InvalidTransition):
        return [DomainError(ErrorCode.INVALID_TRANSITION, str(exc))]
    if isinstance(exc, PermissionDenied):
        return [DomainError(ErrorCode.PERMISSION_DENIED, str(exc))]
    if isinstance(exc, NotFound):
        return [DomainError(ErrorCode.NOT_FOUND, str(exc), exc.field)]
    if isinstance(exc, ObjectDoesNotExist):
        return [DomainError(ErrorCode.NOT_FOUND, str(exc) or "Not found.")]
    raise exc


ErrorCodeEnum = GraphQLEnumType("ErrorCode", ErrorCode, description="Kind of mutation failure.")
DomainErrorType = GraphQLObjectType(
    "DomainError",
    {
        "code": GraphQLField(GraphQLNonNull(ErrorCodeEnum)),
        "message": GraphQLField(GraphQLNonNull(GraphQLString)),
        "field": GraphQLField(
            GraphQLString, description="Path of the input value at fault, when there is one."
        ),
    },
    description="A typed failure reported in a mutation payload.",
)
