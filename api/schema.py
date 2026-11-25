"""GraphQL schema: types derived from the models, scoped through the read services."""

from collections.abc import Callable
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db.models import Model, QuerySet
from django_graphex.core import ObjectType
from django_graphex.fields import DjangoListObjectField
from django_graphex.paginations import CursorGraphqlPagination
from django_graphex.schema import DjangoGraphQLSchema
from django_graphex.types import DjangoListObjectType, DjangoObjectType
from graphql import GraphQLError, GraphQLResolveInfo

from billing.models import (
    Customer,
    Invoice,
    InvoiceLine,
    Payment,
    PaymentAllocation,
    Product,
)
from billing.services import (
    list_customers,
    list_invoices,
    list_payments,
    list_products,
)
from reconciliation.models import MatchProposal, ReconciliationRun
from reconciliation.services import list_match_proposals, list_reconciliation_runs
from tenancy.permissions import Principal, request_principal

RANGE_LOOKUPS = ("exact", "gt", "gte", "lt", "lte")
CHOICE_LOOKUPS = ("exact", "in")
TEXT_LOOKUPS = ("exact", "icontains")


def _readable[M: Model](
    service: Callable[[Principal | None], QuerySet[M]], info: GraphQLResolveInfo[Any]
) -> QuerySet[M]:
    """Run a read service for the request principal, turning a refusal into a GraphQL error.

    Args:
        service: Read service of the owning app.
        info: Resolve info, whose context is the request.

    Returns:
        The queryset of the rows the principal may read.

    Raises:
        GraphQLError: When the principal may not read. graphex hides any other exception type
            behind a generic internal error, so the refusal is translated.
    """
    try:
        return service(request_principal(info.context))
    except PermissionDenied as exc:
        raise GraphQLError(str(exc), extensions={"code": "PERMISSION_DENIED"}) from exc


class CustomerType(DjangoObjectType):
    """A customer of the tenant."""

    class Meta:
        """Type options."""

        model = Customer
        only_fields = (
            "id",
            "name",
            "tax_id",
            "emails",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "country",
        )
        filter_fields = {"name": TEXT_LOOKUPS, "tax_id": ("exact",)}

    @classmethod
    def get_queryset(cls, queryset: QuerySet[Customer], info: GraphQLResolveInfo[Any]) -> Any:
        """Restrict every customer read to what the request principal may read.

        Args:
            queryset: Base queryset produced by the field.
            info: Resolve info, whose context is the request.

        Returns:
            The base queryset narrowed to the rows of the read service.
        """
        return queryset & _readable(list_customers, info)


class CustomerListType(DjangoListObjectType):
    """A cursor paginated page of customers."""

    class Meta:
        """Type options."""

        model = Customer
        pagination = CursorGraphqlPagination(ordering="id")


class ProductType(DjangoObjectType):
    """A catalog product; the unit price is in integer cents."""

    class Meta:
        """Type options."""

        model = Product
        only_fields = ("id", "name", "description", "unit_price")
        filter_fields = {"name": TEXT_LOOKUPS}

    @classmethod
    def get_queryset(cls, queryset: QuerySet[Product], info: GraphQLResolveInfo[Any]) -> Any:
        """Restrict every product read to what the request principal may read.

        Args:
            queryset: Base queryset produced by the field.
            info: Resolve info, whose context is the request.

        Returns:
            The base queryset narrowed to the rows of the read service.
        """
        return queryset & _readable(list_products, info)


class ProductListType(DjangoListObjectType):
    """A cursor paginated page of products."""

    class Meta:
        """Type options."""

        model = Product
        pagination = CursorGraphqlPagination(ordering="id")


class PaymentType(DjangoObjectType):
    """Money received from a customer, in integer cents."""

    class Meta:
        """Type options."""

        model = Payment
        only_fields = (
            "id",
            "amount",
            "currency",
            "value_date",
            "source",
            "reference",
            "created_at",
        )
        filter_fields = {
            "currency": CHOICE_LOOKUPS,
            "source": CHOICE_LOOKUPS,
            "value_date": RANGE_LOOKUPS,
        }

    @classmethod
    def get_queryset(cls, queryset: QuerySet[Payment], info: GraphQLResolveInfo[Any]) -> Any:
        """Restrict every payment read to what the request principal may read.

        Args:
            queryset: Base queryset produced by the field.
            info: Resolve info, whose context is the request.

        Returns:
            The base queryset narrowed to the rows of the read service.
        """
        return queryset & _readable(list_payments, info)


class PaymentListType(DjangoListObjectType):
    """A cursor paginated page of payments, newest first."""

    class Meta:
        """Type options."""

        model = Payment
        pagination = CursorGraphqlPagination(ordering="-created_at")


class InvoiceLineType(DjangoObjectType):
    """A priced line of an invoice, in integer cents."""

    class Meta:
        """Type options."""

        model = InvoiceLine
        only_fields = (
            "id",
            "position",
            "description",
            "quantity",
            "unit_price",
            "discount_bp",
            "net",
            "total",
        )

    @classmethod
    def get_queryset(cls, queryset: QuerySet[InvoiceLine], info: GraphQLResolveInfo[Any]) -> Any:
        """Keep the lines of the invoices the request principal may read.

        Args:
            queryset: Base queryset produced by the field.
            info: Resolve info, whose context is the request.

        Returns:
            The base queryset narrowed to the lines of readable invoices.
        """
        return queryset.filter(invoice__in=_readable(list_invoices, info))


class PaymentAllocationType(DjangoObjectType):
    """The part of a payment applied to an invoice, in integer cents."""

    class Meta:
        """Type options."""

        model = PaymentAllocation
        only_fields = ("id", "amount", "payment")

    @classmethod
    def get_queryset(
        cls, queryset: QuerySet[PaymentAllocation], info: GraphQLResolveInfo[Any]
    ) -> Any:
        """Keep the allocations to the invoices the request principal may read.

        Args:
            queryset: Base queryset produced by the field.
            info: Resolve info, whose context is the request.

        Returns:
            The base queryset narrowed to the allocations of readable invoices.
        """
        return queryset.filter(invoice__in=_readable(list_invoices, info))


class InvoiceType(DjangoObjectType):
    """An invoice with its customer, lines and payment allocations; money is in integer cents."""

    class Meta:
        """Type options."""

        model = Invoice
        only_fields = (
            "id",
            "kind",
            "status",
            "currency",
            "number",
            "year",
            "reference",
            "issue_date",
            "due_date",
            "subtotal",
            "tax_total",
            "total",
            "created_at",
            "customer",
            "lines",
            "allocations",
        )
        filter_fields = {
            "status": CHOICE_LOOKUPS,
            "kind": CHOICE_LOOKUPS,
            "currency": CHOICE_LOOKUPS,
            "number": RANGE_LOOKUPS,
            "issue_date": RANGE_LOOKUPS,
            "due_date": RANGE_LOOKUPS,
        }

    @classmethod
    def get_queryset(cls, queryset: QuerySet[Invoice], info: GraphQLResolveInfo[Any]) -> Any:
        """Restrict every invoice read to what the request principal may read.

        Args:
            queryset: Base queryset produced by the field.
            info: Resolve info, whose context is the request.

        Returns:
            The base queryset narrowed to the rows of the read service.
        """
        return queryset & _readable(list_invoices, info)


class InvoiceListType(DjangoListObjectType):
    """A cursor paginated page of invoices, newest first."""

    class Meta:
        """Type options."""

        model = Invoice
        pagination = CursorGraphqlPagination(ordering="-created_at")


class ReconciliationRunType(DjangoObjectType):
    """One reconciliation run and its counters."""

    class Meta:
        """Type options."""

        model = ReconciliationRun
        only_fields = (
            "id",
            "status",
            "transactions_considered",
            "invoices_considered",
            "auto_count",
            "applied_count",
            "suggested_count",
            "ambiguous_count",
            "unmatched_transactions",
            "unmatched_invoices",
            "duration_ms",
            "error",
            "created_at",
            "completed_at",
        )
        filter_fields = {"status": CHOICE_LOOKUPS}

    @classmethod
    def get_queryset(
        cls, queryset: QuerySet[ReconciliationRun], info: GraphQLResolveInfo[Any]
    ) -> Any:
        """Restrict every run read to what the request principal may read.

        Args:
            queryset: Base queryset produced by the field.
            info: Resolve info, whose context is the request.

        Returns:
            The base queryset narrowed to the rows of the read service.
        """
        return queryset & _readable(list_reconciliation_runs, info)


class ReconciliationRunListType(DjangoListObjectType):
    """A cursor paginated page of reconciliation runs, newest first."""

    class Meta:
        """Type options."""

        model = ReconciliationRun
        pagination = CursorGraphqlPagination(ordering="-created_at")


class MatchProposalType(DjangoObjectType):
    """A proposed match between bank transactions and invoices."""

    class Meta:
        """Type options."""

        model = MatchProposal
        only_fields = (
            "id",
            "kind",
            "engine_status",
            "score",
            "residual",
            "state",
            "decided_at",
            "created_at",
        )
        filter_fields = {
            "kind": CHOICE_LOOKUPS,
            "engine_status": CHOICE_LOOKUPS,
            "state": CHOICE_LOOKUPS,
        }

    @classmethod
    def get_queryset(cls, queryset: QuerySet[MatchProposal], info: GraphQLResolveInfo[Any]) -> Any:
        """Restrict every proposal read to what the request principal may read.

        Args:
            queryset: Base queryset produced by the field.
            info: Resolve info, whose context is the request.

        Returns:
            The base queryset narrowed to the rows of the read service.
        """
        return queryset & _readable(list_match_proposals, info)


class MatchProposalListType(DjangoListObjectType):
    """A cursor paginated page of match proposals, newest first."""

    class Meta:
        """Type options."""

        model = MatchProposal
        pagination = CursorGraphqlPagination(ordering="-created_at")


class Query(ObjectType):
    """Root query type."""

    invoices = DjangoListObjectField(InvoiceListType)
    customers = DjangoListObjectField(CustomerListType)
    products = DjangoListObjectField(ProductListType)
    payments = DjangoListObjectField(PaymentListType)
    reconciliation_runs = DjangoListObjectField(ReconciliationRunListType)
    match_proposals = DjangoListObjectField(MatchProposalListType)


schema = DjangoGraphQLSchema(query=Query)
