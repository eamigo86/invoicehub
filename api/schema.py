"""GraphQL schema: the root query and mutation types over the model types."""

from django_graphex.core import ObjectType
from django_graphex.fields import DjangoListObjectField
from django_graphex.schema import DjangoGraphQLSchema

from api.mutations import Mutation
from api.types import (
    CustomerListType,
    InvoiceListType,
    MatchProposalListType,
    PaymentListType,
    ProductListType,
    ReconciliationRunListType,
)


class Query(ObjectType):
    """Root query type."""

    invoices = DjangoListObjectField(InvoiceListType)
    customers = DjangoListObjectField(CustomerListType)
    products = DjangoListObjectField(ProductListType)
    payments = DjangoListObjectField(PaymentListType)
    reconciliation_runs = DjangoListObjectField(ReconciliationRunListType)
    match_proposals = DjangoListObjectField(MatchProposalListType)


schema = DjangoGraphQLSchema(query=Query, mutation=Mutation)
