"""GraphQL mutations: each one validates its input and calls a service, nothing else.

Every payload carries the result, null on failure, and errors, which is empty on success. Rows
named by an id are looked up through the read services, so an id of another tenant is
indistinguishable from one that does not exist.
"""

import uuid
from collections.abc import Callable
from typing import Any, Self

from django.db import transaction
from django.db.models import Model, QuerySet
from django_graphex.core import InputType, ObjectType, field
from django_graphex.core import Mutation as PayloadBase
from django_graphex.core.base import compile_all_inputs
from graphql import (
    GraphQLArgument,
    GraphQLID,
    GraphQLList,
    GraphQLNonNull,
    GraphQLResolveInfo,
)

from api.errors import HANDLED, DomainError, DomainErrorType, NotFound, domain_errors
from api.inputs import CustomerInput, DraftInvoiceInput, InvoiceReference, SendInvoiceInput
from api.types import CustomerType, InvoiceType
from billing.models import Customer, Invoice
from billing.services import (
    DraftLine,
    delete_draft,
    issue_invoice,
    list_customers,
    list_invoices,
    list_products,
    list_tax_rates,
    save_customer,
    save_draft,
    send_invoice,
)
from tenancy.permissions import Principal, request_principal

# graphex compiles the input types when the apps are ready, before ours are defined.
compile_all_inputs()


def _input_argument(model: type[InputType]) -> Callable[[], GraphQLArgument]:
    """Declare a required argument whose type is the input type compiled from a model.

    Args:
        model: Input model of the argument.

    Returns:
        A thunk graphex calls once the input types are compiled.
    """
    return lambda: GraphQLArgument(GraphQLNonNull(model._meta.graphql_input_type))  # type: ignore[attr-defined]


def _attempt[T](action: Callable[[], T]) -> tuple[T | None, list[DomainError]]:
    """Run a service call in a savepoint, turning a domain failure into payload errors.

    The savepoint keeps the request transaction usable after a failure, and nothing the call
    wrote before failing survives. Exceptions that are not domain errors propagate.

    Args:
        action: Work to run, which calls services.

    Returns:
        The result and no errors, or None and the errors.
    """
    try:
        with transaction.atomic():
            return action(), []
    except HANDLED as exc:
        return None, domain_errors(exc)


def _find[M: Model](
    service: Callable[[Principal | None], QuerySet[M]],
    principal: Principal | None,
    pk: int | uuid.UUID,
    field_path: str,
    label: str,
) -> M:
    """Look up a row through a read service.

    Args:
        service: Read service of the owning app.
        principal: Acting principal.
        pk: Primary key given by the caller.
        field_path: GraphQL path of the id, reported when the row is not found.
        label: Name of the kind of row, used in the message.

    Returns:
        The row.

    Raises:
        NotFound: When the principal may read no such row.
    """
    row = service(principal).filter(pk=pk).first()
    if row is None:
        raise NotFound(f"{label} not found.", field_path)
    return row


class Payload(PayloadBase):
    """Base of the mutation payloads: the typed errors of the call."""

    class Meta:
        """Marks the base as abstract."""

        abstract = True

    errors = field(GraphQLNonNull(GraphQLList(GraphQLNonNull(DomainErrorType))))

    @classmethod
    def reply(cls, **values: Any) -> Self:
        """Build the payload from its result and errors.

        Args:
            **values: Values of the payload fields by name.

        Returns:
            The payload.
        """
        return cls(**values)


class SaveCustomerPayload(Payload):
    """The customer that was saved, or the reasons it was not."""

    customer = field(CustomerType)

    class Arguments:
        """Arguments of saveCustomer."""

        input = _input_argument(CustomerInput)

    @classmethod
    def mutate(cls, root: Any, info: GraphQLResolveInfo[Any], **args: Any) -> Any:
        """Create a customer, or replace the fields of the one named by the id.

        Args:
            root: Unused root value.
            info: Resolve info, whose context is the request.
            **args: Arguments of the field, holding the input.

        Returns:
            The payload.
        """
        principal = request_principal(info.context)

        def save() -> Customer:
            data = CustomerInput.model_validate(args["input"])
            customer = (
                Customer()
                if data.id is None
                else _find(list_customers, principal, data.id, "id", "Customer")
            )
            for name, value in data.model_dump(exclude={"id"}).items():
                setattr(customer, name, value)
            return save_customer(principal, customer)

        customer, errors = _attempt(save)
        return cls.reply(customer=customer, errors=errors)


class SaveDraftPayload(Payload):
    """The draft invoice that was saved, or the reasons it was not."""

    invoice = field(InvoiceType)

    class Arguments:
        """Arguments of saveDraft."""

        input = _input_argument(DraftInvoiceInput)

    @classmethod
    def mutate(cls, root: Any, info: GraphQLResolveInfo[Any], **args: Any) -> Any:
        """Create a draft invoice, or replace the fields and lines of the draft named by the id.

        Args:
            root: Unused root value.
            info: Resolve info, whose context is the request.
            **args: Arguments of the field, holding the input.

        Returns:
            The payload.
        """
        principal = request_principal(info.context)

        def save() -> Invoice:
            data = DraftInvoiceInput.model_validate(args["input"])
            invoice = (
                Invoice()
                if data.id is None
                else _find(list_invoices, principal, data.id, "id", "Invoice")
            )
            invoice.customer = _find(
                list_customers, principal, data.customer_id, "customerId", "Customer"
            )
            invoice.currency = data.currency or invoice.currency
            invoice.due_date = data.due_date
            invoice.notes = data.notes
            lines = []
            for index, line in enumerate(data.lines):
                path = f"lines[{index}]"
                product = (
                    None
                    if line.product_id is None
                    else _find(
                        list_products, principal, line.product_id, f"{path}.productId", "Product"
                    )
                )
                rates = tuple(
                    _find(list_tax_rates, principal, pk, f"{path}.taxRateIds", "Tax rate")
                    for pk in line.tax_rate_ids
                )
                lines.append(
                    DraftLine(
                        description=line.description,
                        quantity=line.quantity,
                        unit_price=line.unit_price,
                        discount_bp=line.discount_bp,
                        tax_rates=rates,
                        product=product,
                    )
                )
            return save_draft(principal, invoice, lines)

        invoice, errors = _attempt(save)
        return cls.reply(invoice=invoice, errors=errors)


class DeleteDraftPayload(Payload):
    """The id of the draft that was deleted, or the reasons it was not."""

    id = field(GraphQLID)

    class Arguments:
        """Arguments of deleteDraft."""

        id = GraphQLArgument(GraphQLNonNull(GraphQLID))

    @classmethod
    def mutate(cls, root: Any, info: GraphQLResolveInfo[Any], **args: Any) -> Any:
        """Delete a draft invoice with its lines.

        Args:
            root: Unused root value.
            info: Resolve info, whose context is the request.
            **args: Arguments of the field, holding the id.

        Returns:
            The payload.
        """
        principal = request_principal(info.context)

        def delete() -> str:
            reference = InvoiceReference.model_validate({"id": args["id"]})
            invoice = _find(list_invoices, principal, reference.id, "id", "Invoice")
            delete_draft(principal, invoice)
            return str(reference.id)

        deleted, errors = _attempt(delete)
        return cls.reply(id=deleted, errors=errors)


class IssueInvoicePayload(Payload):
    """The invoice that was issued, or the reasons it was not."""

    invoice = field(InvoiceType)

    class Arguments:
        """Arguments of issueInvoice."""

        id = GraphQLArgument(GraphQLNonNull(GraphQLID))

    @classmethod
    def mutate(cls, root: Any, info: GraphQLResolveInfo[Any], **args: Any) -> Any:
        """Issue a draft invoice or credit note, which numbers and freezes it.

        Args:
            root: Unused root value.
            info: Resolve info, whose context is the request.
            **args: Arguments of the field, holding the id.

        Returns:
            The payload.
        """
        principal = request_principal(info.context)

        def issue() -> Invoice:
            reference = InvoiceReference.model_validate({"id": args["id"]})
            invoice = _find(list_invoices, principal, reference.id, "id", "Invoice")
            return issue_invoice(principal, invoice)

        invoice, errors = _attempt(issue)
        return cls.reply(invoice=invoice, errors=errors)


class SendInvoicePayload(Payload):
    """The invoice whose email was requested, or the reasons it was not."""

    invoice = field(InvoiceType)

    class Arguments:
        """Arguments of sendInvoice."""

        input = _input_argument(SendInvoiceInput)

    @classmethod
    def mutate(cls, root: Any, info: GraphQLResolveInfo[Any], **args: Any) -> Any:
        """Request that an issued invoice is emailed, once per idempotency key.

        Args:
            root: Unused root value.
            info: Resolve info, whose context is the request.
            **args: Arguments of the field, holding the input.

        Returns:
            The payload.
        """
        principal = request_principal(info.context)

        def send() -> Invoice:
            data = SendInvoiceInput.model_validate(args["input"])
            invoice = _find(list_invoices, principal, data.id, "id", "Invoice")
            send_invoice(principal, invoice, data.idempotency_key, data.recipients)
            return invoice

        invoice, errors = _attempt(send)
        return cls.reply(invoice=invoice, errors=errors)


class Mutation(ObjectType):
    """Root mutation type."""

    save_customer = SaveCustomerPayload.Field()
    save_draft = SaveDraftPayload.Field()
    delete_draft = DeleteDraftPayload.Field()
    issue_invoice = IssueInvoicePayload.Field()
    send_invoice = SendInvoicePayload.Field()
