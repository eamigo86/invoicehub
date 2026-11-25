"""The GraphQL endpoint never shows a token the invoices of another tenant."""

import json

import pytest
from django.test import Client

from accounts.models import User
from billing.models import Customer, Invoice
from core.models import Currency
from tenancy.context import tenant_context
from tenancy.models import Membership, Tenant
from tenancy.permissions import Principal
from tenancy.tokens import issue_token

pytestmark = [pytest.mark.isolation, pytest.mark.django_db(transaction=True)]

QUERY = "{ invoices { totalCount results(first: 50) { reference } } }"


def tenant_with_token(name: str) -> str:
    """Create a tenant with one invoice and a read token.

    Args:
        name: Tenant name, also the reference of its invoice.

    Returns:
        The plaintext token.
    """
    tenant = Tenant.objects.create(name=name)
    with tenant_context(tenant.pk):
        customer = Customer.objects.create(name=f"customer of {name}")
        Invoice.objects.create(customer=customer, currency=Currency.USD, reference=name)
    user = User.objects.create_user(f"owner-{name}", f"owner-{name}@example.com", "pw")
    membership = Membership.objects.create(user=user, tenant=tenant, role=Membership.Role.OWNER)
    return issue_token(Principal.from_membership(membership), "ci", ["invoices:read"])[1]


def run(query: str, token: str) -> dict[str, object]:
    """Send a query to the endpoint with a Bearer token.

    Args:
        query: GraphQL document.
        token: Plaintext API token.

    Returns:
        The decoded JSON response.
    """
    response = Client().post(
        "/graphql/",
        data=json.dumps({"query": query}),
        content_type="application/json",
        headers={"Authorization": f"Bearer {token}"},
    )
    body: dict[str, object] = response.json()
    return body


def test_the_token_of_a_sees_only_the_invoices_of_a() -> None:
    """Query.invoices with A's token returns A's invoice and counts only A's rows."""
    tenant_with_token("B")
    token_a = tenant_with_token("A")
    body = run(QUERY, token_a)
    assert body == {"data": {"invoices": {"totalCount": 1, "results": [{"reference": "A"}]}}}


def test_the_query_takes_no_tenant_argument() -> None:
    """The tenant comes from the token: the only argument of invoices is the filter."""
    from api.schema import schema

    field = schema.graphql_schema.query_type.fields["invoices"]
    assert list(field.args) == ["filter"]
