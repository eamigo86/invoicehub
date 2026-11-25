"""The GraphQL endpoint never shows a token the rows of another tenant."""

import json

import pytest
from django.test import Client

from accounts.models import User
from billing.models import Customer, Invoice, Payment, Product
from core.models import Currency
from reconciliation.models import MatchProposal, ReconciliationRun
from tenancy.context import tenant_context
from tenancy.models import Membership, Tenant, TenantModel
from tenancy.permissions import Principal
from tenancy.tokens import issue_token
from tests.test_api_queries import make_world

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


ROOT_MODELS: dict[str, type[TenantModel]] = {
    "customers": Customer,
    "products": Product,
    "payments": Payment,
    "reconciliationRuns": ReconciliationRun,
    "matchProposals": MatchProposal,
}


@pytest.mark.parametrize("root", ROOT_MODELS)
def test_each_root_query_returns_only_the_rows_and_count_of_the_token_tenant(root: str) -> None:
    """With B's data present, A's token gets exactly A's ids and A's totalCount.

    Args:
        root: Root query field under test.
    """
    token_a = make_world("A", invoices=2)
    make_world("B", invoices=3)
    tenant_a = Tenant.objects.get(name="A")
    with tenant_context(tenant_a.pk):
        expected = {str(pk) for pk in ROOT_MODELS[root].objects.values_list("pk", flat=True)}
    body = run(f"{{ {root} {{ totalCount results(first: 100) {{ id }} }} }}", token_a)
    page = body["data"][root]  # type: ignore[index]
    assert {row["id"] for row in page["results"]} == expected
    assert page["totalCount"] == len(expected)


def test_nested_fields_of_a_never_reach_rows_of_b() -> None:
    """Customers, lines and payments reached through A's invoices are all A's."""
    token_a = make_world("A", invoices=2)
    make_world("B", invoices=3)
    query = """
    { invoices { totalCount results(first: 100) {
        customer { name }
        lines { totalCount results { description } }
        allocations { totalCount results { payment { reference } } }
    } } }
    """
    page = run(query, token_a)["data"]["invoices"]  # type: ignore[index]
    assert page["totalCount"] == 2
    for invoice in page["results"]:
        assert invoice["customer"]["name"].startswith("A ")
        assert invoice["lines"]["totalCount"] == 2
        assert all(row["description"].startswith("A ") for row in invoice["lines"]["results"])
        assert invoice["allocations"]["totalCount"] == 1
        for allocation in invoice["allocations"]["results"]:
            assert allocation["payment"]["reference"].startswith("A-")
