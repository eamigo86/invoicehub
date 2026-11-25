"""The GraphQL endpoint: token-only access, the invoices query, filters and cursor pages."""

import json
from datetime import date
from typing import Any

import pytest
from django.test import Client

from accounts.models import User
from billing.models import Customer, Invoice
from core.models import Currency
from tenancy.context import tenant_context
from tenancy.models import Membership, Tenant
from tenancy.permissions import Principal
from tenancy.tokens import issue_token

pytestmark = pytest.mark.django_db(transaction=True)

URL = "/graphql/"


def make_world(name: str, scopes: list[str]) -> tuple[Tenant, str]:
    """Create a tenant with three invoices and an API token.

    Args:
        name: Tenant name, also used to name the user.
        scopes: Scopes granted to the token.

    Returns:
        The tenant and the plaintext token.
    """
    tenant = Tenant.objects.create(name=name)
    with tenant_context(tenant.pk):
        customer = Customer.objects.create(name=f"customer of {name}")
        for status, due, total in [
            (Invoice.Status.ISSUED, date(2026, 10, 1), 1000),
            (Invoice.Status.PARTIALLY_PAID, date(2026, 10, 15), 2000),
            (Invoice.Status.PAID, date(2026, 12, 1), 3000),
        ]:
            Invoice.objects.create(
                customer=customer,
                currency=Currency.USD,
                status=status,
                due_date=due,
                total=total,
                reference=f"{name}-{total}",
            )
    user = User.objects.create_user(f"owner-{name}", f"owner-{name}@example.com", "pw")
    membership = Membership.objects.create(user=user, tenant=tenant, role=Membership.Role.OWNER)
    _, plaintext = issue_token(Principal.from_membership(membership), "ci", scopes)
    return tenant, plaintext


def post(query: str, token: str | None = None, **variables: Any) -> Any:
    """Send a GraphQL query.

    Args:
        query: GraphQL document.
        token: Plaintext API token, or None to send no credentials.
        **variables: GraphQL variables.

    Returns:
        The test client response.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return Client().post(
        URL,
        data=json.dumps({"query": query, "variables": variables}),
        content_type="application/json",
        headers=headers,
    )


def test_a_request_without_a_token_gets_401() -> None:
    """Anonymous requests are refused with a JSON body."""
    response = post("{ invoices { totalCount } }")
    assert response.status_code == 401
    assert response["Content-Type"] == "application/json"
    assert "detail" in response.json()


def test_a_session_user_without_a_token_gets_401() -> None:
    """A signed in member with a tenant in the session still needs a token."""
    tenant, _ = make_world("A", ["invoices:read"])
    client = Client()
    client.force_login(User.objects.get(username="owner-A"))
    session = client.session
    session["tenant_id"] = str(tenant.pk)
    session.save()
    response = client.post(
        URL,
        data=json.dumps({"query": "{ invoices { totalCount } }"}),
        content_type="application/json",
    )
    assert response.status_code == 401


def test_a_token_reads_its_invoices_with_scalar_fields() -> None:
    """The invoices query returns the scalar fields in cents, scoped to the token tenant."""
    _, token = make_world("A", ["invoices:read"])
    response = post(
        "{ invoices { totalCount results(first: 10) { id status kind currency total "
        "subtotal taxTotal dueDate issueDate number } } }",
        token,
    )
    assert response.status_code == 200, response.content
    data = response.json()["data"]["invoices"]
    assert data["totalCount"] == 3
    # graphex serializes BigInt as a decimal string, so cents arrive as strings.
    assert sorted(r["total"] for r in data["results"]) == ["1000", "2000", "3000"]
    assert {r["currency"] for r in data["results"]} == {"USD"}
    assert {r["status"] for r in data["results"]} == {"ISSUED", "PARTIALLY_PAID", "PAID"}


def test_filters_combine_a_status_in_lookup_and_a_due_date_lookup() -> None:
    """The RFC example filter, in graphex syntax, selects the overdue invoices."""
    _, token = make_world("A", ["invoices:read"])
    query = """
    query Overdue($statuses: [billingInvoiceStatusEnum], $before: Date) {
      invoices(filter: {status: {in: $statuses}, dueDate: {lt: $before}}) {
        totalCount
        results(first: 10) { total status }
      }
    }
    """
    response = post(query, token, statuses=["ISSUED", "PARTIALLY_PAID"], before="2026-10-10")
    assert response.status_code == 200, response.content
    data = response.json()["data"]["invoices"]
    assert data["results"] == [{"total": "1000", "status": "ISSUED"}]
    assert data["totalCount"] == 1


def test_cursor_pagination_walks_every_invoice_once() -> None:
    """Following endCursor visits each invoice exactly once and then stops."""
    _, token = make_world("A", ["invoices:read"])
    query = """
    query Page($cursor: String) {
      invoices {
        results(first: 2, cursor: $cursor) { total }
        pageInfo(first: 2, cursor: $cursor) { hasNextPage endCursor }
      }
    }
    """
    seen: list[str] = []
    cursor = None
    for _ in range(5):
        data = post(query, token, cursor=cursor).json()["data"]["invoices"]
        seen += [r["total"] for r in data["results"]]
        if not data["pageInfo"]["hasNextPage"]:
            break
        cursor = data["pageInfo"]["endCursor"]
    assert sorted(seen) == ["1000", "2000", "3000"]


def test_page_size_defaults_to_50_and_is_capped_at_100() -> None:
    """Omitting first returns 50 rows and asking for more than the maximum returns 100."""
    tenant, token = make_world("A", ["invoices:read"])
    with tenant_context(tenant.pk):
        customer = Customer.objects.get()
        for _ in range(120):
            Invoice.objects.create(customer=customer, currency=Currency.USD)

    def count(arguments: str) -> int:
        data = post(f"{{ invoices {{ results{arguments} {{ total }} }} }}", token).json()
        return len(data["data"]["invoices"]["results"])

    assert count("") == 50
    assert count("(first: 100000)") == 100


def test_a_token_without_invoices_read_gets_a_graphql_error() -> None:
    """The read service refuses the token and the error reaches the client."""
    _, token = make_world("A", ["invoices:write"])
    response = post("{ invoices { totalCount } }", token)
    body = response.json()
    assert body.get("data", {}).get("invoices") is None
    assert "invoices:read" in body["errors"][0]["message"]
    assert body["errors"][0]["extensions"]["code"] == "PERMISSION_DENIED"
