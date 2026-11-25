"""The GraphQL endpoint never shows a token the rows of another tenant."""

import json
from unittest.mock import MagicMock

import pytest
from django.test import Client

from accounts.models import User
from billing.models import Customer, Invoice, InvoiceEmail, Payment, Product, TaxRate
from core.models import Currency
from reconciliation.models import MatchProposal, ReconciliationRun
from tenancy.context import tenant_context
from tenancy.models import Membership, Tenant, TenantModel
from tenancy.permissions import Principal
from tenancy.tokens import issue_token
from tests.test_api_actions import (
    ACCEPT,
    ALL_SCOPES,
    CREDIT,
    ISSUE,
    REJECT,
    SEND,
    draft_for,
    issued_invoice,
    pending_proposal,
    state_of,
)
from tests.test_api_actions import make_world as make_issuer_world
from tests.test_api_graphql import make_world as make_invoice_world
from tests.test_api_mutations import DELETE_DRAFT, SAVE_CUSTOMER, SAVE_DRAFT, catalog
from tests.test_api_mutations import run as mutate
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


def two_writers() -> tuple[tuple[Tenant, str], tuple[Tenant, str]]:
    """Create tenants A and B, each with a catalog, a draft and a token that reads and writes.

    Returns:
        Each tenant with its plaintext token, A first.
    """
    worlds = []
    for name in ("A", "B"):
        tenant, token = make_invoice_world(name, ["invoices:read", "invoices:write"])
        worlds.append((tenant, token))
    return worlds[0], worlds[1]


def draft_of(tenant: Tenant, token: str) -> Invoice:
    """Save a draft through the API for a tenant.

    Args:
        tenant: Tenant of the token.
        token: Plaintext token that reads and writes.

    Returns:
        The stored draft.
    """
    customer, _, _ = catalog(tenant)
    saved = mutate(SAVE_DRAFT, token, input=draft_input_for(customer))["saveDraft"]
    with tenant_context(tenant.pk):
        invoice: Invoice = Invoice.objects.get(pk=saved["invoice"]["id"])
    return invoice


def draft_input_for(customer: Customer, **changes: object) -> dict[str, object]:
    """Build a saveDraft input with one line.

    Args:
        customer: Customer to invoice.
        **changes: Members to add to or replace in the input.

    Returns:
        The input value.
    """
    base: dict[str, object] = {
        "customerId": str(customer.pk),
        "lines": [{"description": "Setup", "quantity": "1", "unitPrice": "1000"}],
    }
    return {**base, **changes}


def test_a_cannot_update_the_customer_of_b_by_id() -> None:
    """Mutation.saveCustomer with B's customer id is NOT_FOUND and leaves B's row unchanged."""
    (_, token_a), (tenant_b, _) = two_writers()
    with tenant_context(tenant_b.pk):
        customer_b = Customer.objects.get()
    data = mutate(SAVE_CUSTOMER, token_a, input={"id": str(customer_b.pk), "name": "Hijacked"})
    assert [e["code"] for e in data["saveCustomer"]["errors"]] == ["NOT_FOUND"]
    assert data["saveCustomer"]["customer"] is None
    with tenant_context(tenant_b.pk):
        assert Customer.objects.get(pk=customer_b.pk).name == customer_b.name


def test_a_creating_a_customer_never_touches_the_rows_of_b() -> None:
    """Mutation.saveCustomer without an id creates the row in A, and B keeps its own."""
    (tenant_a, token_a), (tenant_b, _) = two_writers()
    mutate(SAVE_CUSTOMER, token_a, input={"name": "New"})
    with tenant_context(tenant_a.pk):
        assert Customer.objects.filter(name="New").count() == 1
    with tenant_context(tenant_b.pk):
        assert not Customer.objects.filter(name="New").exists()


def test_a_cannot_edit_the_draft_of_b_by_id() -> None:
    """Mutation.saveDraft with B's draft id is NOT_FOUND and leaves B's draft unchanged."""
    (tenant_a, token_a), (tenant_b, token_b) = two_writers()
    draft_b = draft_of(tenant_b, token_b)
    customer_a, _, _ = catalog(tenant_a)
    edit = draft_input_for(
        customer_a,
        id=str(draft_b.pk),
        notes="Hijacked",
        lines=[{"description": "Stolen", "quantity": "1", "unitPrice": "1"}],
    )
    data = mutate(SAVE_DRAFT, token_a, input=edit)["saveDraft"]
    assert [(e["code"], e["field"]) for e in data["errors"]] == [("NOT_FOUND", "id")]
    with tenant_context(tenant_b.pk):
        stored = Invoice.objects.get(pk=draft_b.pk)
        assert stored.notes == draft_b.notes
        assert [line.description for line in stored.lines.all()] == ["Setup"]


def test_a_cannot_attach_the_customer_product_or_tax_rate_of_b_to_a_draft() -> None:
    """Mutation.saveDraft treats the ids of B's rows as unknown, each at its own path."""
    (tenant_a, token_a), (tenant_b, _) = two_writers()
    customer_a, _, _ = catalog(tenant_a)
    customer_b, product_b, rate_b = catalog(tenant_b)
    stolen_customer = mutate(SAVE_DRAFT, token_a, input=draft_input_for(customer_b))["saveDraft"]
    assert [(e["code"], e["field"]) for e in stolen_customer["errors"]] == [
        ("NOT_FOUND", "customerId")
    ]
    line = {"description": "x", "quantity": "1", "unitPrice": "1"}
    with_product = draft_input_for(customer_a, lines=[{**line, "productId": str(product_b.pk)}])
    stolen_product = mutate(SAVE_DRAFT, token_a, input=with_product)["saveDraft"]
    assert [(e["code"], e["field"]) for e in stolen_product["errors"]] == [
        ("NOT_FOUND", "lines[0].productId")
    ]
    with_rate = draft_input_for(customer_a, lines=[{**line, "taxRateIds": [str(rate_b.pk)]}])
    stolen_rate = mutate(SAVE_DRAFT, token_a, input=with_rate)["saveDraft"]
    assert [(e["code"], e["field"]) for e in stolen_rate["errors"]] == [
        ("NOT_FOUND", "lines[0].taxRateIds")
    ]
    with tenant_context(tenant_a.pk):
        assert not Invoice.objects.filter(status=Invoice.Status.DRAFT).exists()
    with tenant_context(tenant_b.pk):
        assert TaxRate.objects.filter(pk=rate_b.pk).exists()


def test_a_cannot_delete_the_draft_of_b_by_id() -> None:
    """Mutation.deleteDraft with B's draft id is NOT_FOUND and B's draft stays."""
    (_, token_a), (tenant_b, token_b) = two_writers()
    draft_b = draft_of(tenant_b, token_b)
    data = mutate(DELETE_DRAFT, token_a, id=str(draft_b.pk))["deleteDraft"]
    assert data["id"] is None
    assert [(e["code"], e["field"]) for e in data["errors"]] == [("NOT_FOUND", "id")]
    with tenant_context(tenant_b.pk):
        assert Invoice.objects.filter(pk=draft_b.pk).exists()


def two_issuers() -> tuple[tuple[Tenant, str], tuple[Tenant, str]]:
    """Create tenants A and B with complete issuer details and tokens that read and write.

    Returns:
        Each tenant with its plaintext token, A first.
    """
    a, b = (make_issuer_world(name, ["invoices:read", "invoices:write"]) for name in ("A", "B"))
    return a, b


def test_a_cannot_issue_the_draft_of_b_by_id() -> None:
    """Mutation.issueInvoice with B's draft id is NOT_FOUND and B's draft stays a draft."""
    (_, token_a), (tenant_b, token_b) = two_issuers()
    draft_b = draft_for(tenant_b, token_b)
    data = mutate(ISSUE, token_a, id=draft_b)["issueInvoice"]
    assert data["invoice"] is None
    assert [(e["code"], e["field"]) for e in data["errors"]] == [("NOT_FOUND", "id")]
    with tenant_context(tenant_b.pk):
        stored = Invoice.objects.get(pk=draft_b)
        assert (stored.status, stored.reference) == (Invoice.Status.DRAFT, "")


def test_a_cannot_send_the_invoice_of_b_by_id(enqueued_email: MagicMock) -> None:
    """Mutation.sendInvoice with B's invoice id is NOT_FOUND, stores no request, sends no email.

    Args:
        enqueued_email: Mock standing in for the email task delay.
    """
    (tenant_a, token_a), (tenant_b, token_b) = two_issuers()
    invoice_b = issued_invoice(tenant_b, token_b)
    request = {"id": invoice_b, "idempotencyKey": "k", "recipients": ["x@buyer.test"]}
    data = mutate(SEND, token_a, input=request)["sendInvoice"]
    assert [(e["code"], e["field"]) for e in data["errors"]] == [("NOT_FOUND", "id")]
    enqueued_email.assert_not_called()
    for tenant in (tenant_a, tenant_b):
        with tenant_context(tenant.pk):
            assert not InvoiceEmail.objects.exists()


def test_a_cannot_draft_a_credit_note_for_the_invoice_of_b() -> None:
    """Mutation.draftCreditNote with B's invoice id is NOT_FOUND and no credit note appears."""
    (tenant_a, token_a), (tenant_b, token_b) = two_issuers()
    invoice_b = issued_invoice(tenant_b, token_b)
    data = mutate(CREDIT, token_a, input={"invoiceId": invoice_b})["draftCreditNote"]
    assert data["creditNote"] is None
    assert [(e["code"], e["field"]) for e in data["errors"]] == [("NOT_FOUND", "invoiceId")]
    for tenant in (tenant_a, tenant_b):
        with tenant_context(tenant.pk):
            assert not Invoice.objects.filter(kind=Invoice.Kind.CREDIT_NOTE).exists()


@pytest.mark.parametrize(("query", "root"), [(ACCEPT, "acceptMatch"), (REJECT, "rejectMatch")])
def test_a_cannot_decide_the_proposal_of_b(query: str, root: str) -> None:
    """Mutation.acceptMatch and rejectMatch with B's proposal id are NOT_FOUND, B's stays pending.

    Args:
        query: Mutation document under test.
        root: Mutation field under test.
    """
    tenant_a, token_a, proposal_a = pending_proposal("A", ALL_SCOPES)
    tenant_b, _, proposal_b = pending_proposal("B", ALL_SCOPES)
    data = mutate(query, token_a, id=str(proposal_b.pk))[root]
    assert data["proposal"] is None
    assert [(e["code"], e["field"]) for e in data["errors"]] == [("NOT_FOUND", "id")]
    assert state_of(tenant_b, proposal_b) == "pending"
    assert state_of(tenant_a, proposal_a) == "pending"
    with tenant_context(tenant_b.pk):
        assert not Payment.objects.filter(source=Payment.Source.BANK).exists()
