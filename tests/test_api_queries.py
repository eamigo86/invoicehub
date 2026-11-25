"""The GraphQL root queries for the catalog, payments and reconciliation, and nested fields."""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts.models import User
from billing.models import Customer, Invoice, InvoiceLine, Payment, PaymentAllocation, Product
from core.models import Currency
from reconciliation.models import MatchProposal, ReconciliationRun
from tenancy.context import tenant_context
from tenancy.models import Membership, Tenant
from tenancy.permissions import Principal
from tenancy.tokens import issue_token
from tests.test_api_graphql import post

pytestmark = pytest.mark.django_db(transaction=True)


def make_world(name: str, scopes: list[str] | None = None, invoices: int = 2) -> str:
    """Create a tenant with a catalog, invoices with lines and payments, and a proposal.

    Args:
        name: Tenant name, also used as a prefix of the names it creates.
        scopes: Scopes granted to the token, invoices:read by default.
        invoices: Number of invoices, each with two lines and one allocated payment.

    Returns:
        The plaintext API token.
    """
    tenant = Tenant.objects.create(name=name)
    with tenant_context(tenant.pk):
        for index in range(invoices):
            customer = Customer.objects.create(name=f"{name} customer {index}", tax_id=f"T{index}")
            invoice = Invoice.objects.create(
                customer=customer, currency=Currency.USD, total=3000, reference=f"{name}-{index}"
            )
            for position in (1, 2):
                InvoiceLine.objects.create(
                    invoice=invoice,
                    position=position,
                    description=f"{name} line {position}",
                    quantity=Decimal("1.500"),
                    unit_price=1000,
                    net=1500,
                    total=1500,
                )
            payment = Payment.objects.create(
                amount=3000,
                currency=Currency.USD,
                value_date=date(2026, 10, 1 + index),
                reference=f"{name}-pay-{index}",
            )
            PaymentAllocation.objects.create(payment=payment, invoice=invoice, amount=3000)
        Product.objects.create(name=f"{name} Widget", unit_price=1999)
        run = ReconciliationRun.objects.create(
            config={}, status=ReconciliationRun.Status.COMPLETED, suggested_count=1
        )
        MatchProposal.objects.create(
            run=run,
            kind=MatchProposal.Kind.ONE_TO_ONE,
            engine_status=MatchProposal.EngineStatus.SUGGESTED,
            score=0.8,
            breakdown={},
            residual=0,
        )
        MatchProposal.objects.create(
            run=run,
            kind=MatchProposal.Kind.ONE_TO_MANY,
            engine_status=MatchProposal.EngineStatus.AUTO,
            state=MatchProposal.State.APPLIED,
            score=1.0,
            breakdown={},
            residual=0,
        )
    user = User.objects.create_user(f"owner-{name}", f"owner-{name}@example.com", "pw")
    membership = Membership.objects.create(user=user, tenant=tenant, role=Membership.Role.OWNER)
    return issue_token(Principal.from_membership(membership), "ci", scopes or ["invoices:read"])[1]


def data(query: str, token: str, **variables: Any) -> Any:
    """Run a query that must succeed and return its data.

    Args:
        query: GraphQL document.
        token: Plaintext API token.
        **variables: GraphQL variables.

    Returns:
        The data member of the response.
    """
    body = post(query, token, **variables).json()
    assert "errors" not in body, body
    return body["data"]


def test_customers_are_found_by_a_case_insensitive_name_fragment() -> None:
    """A shop can look a customer up by part of the name before creating one."""
    token = make_world("A")
    query = """
    query Find($fragment: String) {
      customers(filter: {name: {icontains: $fragment}}) {
        totalCount
        results(first: 10) { name taxId country }
      }
    }
    """
    page = data(query, token, fragment="CUSTOMER 1")["customers"]
    assert page == {
        "totalCount": 1,
        "results": [{"name": "A customer 1", "taxId": "T1", "country": "US"}],
    }


def test_products_are_listed_with_their_price_in_cents() -> None:
    """The catalog is readable, with the unit price as integer cents."""
    token = make_world("A")
    page = data("{ products { results(first: 5) { name unitPrice } } }", token)["products"]
    assert page["results"] == [{"name": "A Widget", "unitPrice": "1999"}]


def test_payments_filter_by_value_date_and_currency() -> None:
    """Payments can be selected by a date range and by currency."""
    token = make_world("A")
    query = """
    { payments(filter: {valueDate: {gte: "2026-10-02"}, currency: {in: [USD]}}) {
        totalCount
        results(first: 5) { amount currency valueDate source reference }
    } }
    """
    page = data(query, token)["payments"]
    assert page["totalCount"] == 1
    assert page["results"] == [
        {
            "amount": "3000",
            "currency": "USD",
            "valueDate": "2026-10-02",
            "source": "MANUAL",
            "reference": "A-pay-1",
        }
    ]


def test_reconciliation_runs_expose_their_counters() -> None:
    """A run shows its status and how many proposals it produced."""
    token = make_world("A")
    page = data(
        "{ reconciliationRuns { results(first: 5) { status suggestedCount appliedCount } } }", token
    )["reconciliationRuns"]
    assert page["results"] == [{"status": "COMPLETED", "suggestedCount": 1, "appliedCount": 0}]


def test_match_proposals_filter_by_state() -> None:
    """The review queue is the pending proposals, selected with a state in lookup."""
    token = make_world("A")
    query = """
    { matchProposals(filter: {state: {in: [PENDING]}}) {
        totalCount
        results(first: 5) { kind engineStatus state score residual }
    } }
    """
    page = data(query, token)["matchProposals"]
    assert page["totalCount"] == 1
    assert page["results"][0]["engineStatus"] == "SUGGESTED"
    assert page["results"][0]["state"] == "PENDING"


@pytest.mark.parametrize(
    "root", ["customers", "products", "payments", "reconciliationRuns", "matchProposals"]
)
def test_every_new_query_needs_invoices_read(root: str) -> None:
    """A token without invoices:read gets a permission error from each new root field.

    Args:
        root: Root field under test.
    """
    token = make_world("A", ["invoices:write"])
    body = post(f"{{ {root} {{ totalCount }} }}", token).json()
    assert body["errors"][0]["extensions"]["code"] == "PERMISSION_DENIED"


NESTED = """
{ invoices { results(first: 50) {
    reference
    customer { name }
    lines { totalCount results { position description quantity unitPrice total } }
    allocations { totalCount results { amount payment { amount valueDate reference } } }
} } }
"""


def test_an_invoice_carries_its_customer_lines_and_payments() -> None:
    """The overdue-invoice shape of the RFC: customer, lines and payments in one query."""
    token = make_world("A", invoices=1)
    invoice = data(NESTED, token)["invoices"]["results"][0]
    assert invoice["customer"] == {"name": "A customer 0"}
    assert [line["position"] for line in invoice["lines"]["results"]] == [1, 2]
    assert invoice["lines"]["results"][0] == {
        "position": 1,
        "description": "A line 1",
        "quantity": "1.500",
        "unitPrice": "1000",
        "total": "1500",
    }
    assert invoice["allocations"]["results"] == [
        {
            "amount": "3000",
            "payment": {"amount": "3000", "valueDate": "2026-10-01", "reference": "A-pay-0"},
        }
    ]


def count_queries(token: str) -> int:
    """Count the database queries of the nested invoices query.

    Args:
        token: Plaintext API token.

    Returns:
        The number of queries the whole request ran.
    """
    with CaptureQueriesContext(connection) as captured:
        data(NESTED, token)
    return len(captured)


def test_the_queries_of_a_nested_page_do_not_grow_with_its_size() -> None:
    """Twelve invoices with nested fields cost as many queries as two, and few of them."""
    small = count_queries(make_world("S", invoices=2))
    large = count_queries(make_world("L", invoices=12))
    assert large == small
    assert large <= 9


def test_nested_lists_are_scoped_through_the_read_services() -> None:
    """The lines and allocations of a page are fetched only for readable invoices."""
    token = make_world("A", invoices=1)
    with CaptureQueriesContext(connection) as captured:
        data(NESTED, token)
    statements = [q["sql"] for q in captured if 'FROM "billing_invoiceline"' in q["sql"]]
    assert statements
    assert all('"billing_invoice"' in sql and "IN (SELECT" in sql for sql in statements)


def test_a_customer_hidden_by_its_read_service_is_hidden_inside_an_invoice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The nested customer goes through the customer read service.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    token = make_world("A", invoices=1)
    monkeypatch.setattr("api.schema.list_customers", lambda principal: Customer.objects.none())
    result = data("{ invoices { results(first: 5) { customer { name } } } }", token)
    assert result["invoices"]["results"] == [{"customer": None}]


def test_a_payment_hidden_by_its_read_service_is_hidden_inside_an_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The payment of an allocation goes through the payment read service.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    token = make_world("A", invoices=1)
    monkeypatch.setattr("api.schema.list_payments", lambda principal: Payment.objects.none())
    query = (
        "{ invoices { results(first: 5) { allocations { results { amount payment { id } } } } } }"
    )
    allocation = data(query, token)["invoices"]["results"][0]["allocations"]["results"][0]
    assert allocation == {"amount": "3000", "payment": None}
