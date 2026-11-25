"""GraphQL schema: types derived from the models, resolved through the billing services."""

from typing import Any

from django.core.exceptions import PermissionDenied
from django.db.models import QuerySet
from django_graphex.core import ObjectType
from django_graphex.fields import DjangoListObjectField
from django_graphex.paginations import CursorGraphqlPagination
from django_graphex.schema import DjangoGraphQLSchema
from django_graphex.types import DjangoListObjectType, DjangoObjectType
from graphql import GraphQLError, GraphQLResolveInfo

from billing.models import Invoice
from billing.services import list_invoices
from tenancy.permissions import request_principal

LOOKUPS_BY_FIELD = {
    "status": ("exact", "in"),
    "kind": ("exact", "in"),
    "currency": ("exact", "in"),
    "number": ("exact", "gt", "gte", "lt", "lte"),
    "issue_date": ("exact", "gt", "gte", "lt", "lte"),
    "due_date": ("exact", "gt", "gte", "lt", "lte"),
}


class InvoiceType(DjangoObjectType):
    """An invoice with its scalar fields; money is in integer cents."""

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
        )
        filter_fields = LOOKUPS_BY_FIELD

    @classmethod
    def get_queryset(cls, queryset: QuerySet[Invoice], info: GraphQLResolveInfo[Any]) -> Any:
        """Restrict every invoice read to what the request principal may read.

        Args:
            queryset: Base queryset produced by the field.
            info: Resolve info, whose context is the request.

        Returns:
            The base queryset narrowed to the rows of the read service.

        Raises:
            GraphQLError: When the principal may not read invoices. graphex hides any other
                exception type behind a generic internal error, so the refusal is translated.
        """
        try:
            readable = list_invoices(request_principal(info.context))
        except PermissionDenied as exc:
            raise GraphQLError(str(exc), extensions={"code": "PERMISSION_DENIED"}) from exc
        return queryset & readable


class InvoiceListType(DjangoListObjectType):
    """A cursor paginated page of invoices, newest first."""

    class Meta:
        """Type options."""

        model = Invoice
        pagination = CursorGraphqlPagination(ordering="-created_at")


class Query(ObjectType):
    """Root query type."""

    invoices = DjangoListObjectField(InvoiceListType)


schema = DjangoGraphQLSchema(query=Query)
