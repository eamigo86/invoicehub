"""The GraphQL mutations: saving customers, saving and deleting drafts, and typed errors."""

from typing import Any

import pytest

from billing.models import AuditEvent, Customer, Invoice, InvoiceLine, Product, TaxRate
from core.models import Currency
from tenancy.context import tenant_context
from tenancy.models import ApiToken, Tenant
from tests.test_api_graphql import make_world, post

pytestmark = pytest.mark.django_db(transaction=True)

READ_WRITE = ["invoices:read", "invoices:write"]
ERRORS = "errors { code message field }"


def run(query: str, token: str, **variables: Any) -> dict[str, Any]:
    """Send a mutation and return its data, requiring a successful HTTP response.

    Args:
        query: GraphQL document.
        token: Plaintext API token.
        **variables: GraphQL variables.

    Returns:
        The data member of the response.
    """
    response = post(query, token, **variables)
    assert response.status_code == 200, response.content
    body = response.json()
    assert "errors" not in body, body
    data: dict[str, Any] = body["data"]
    return data


SAVE_CUSTOMER = f"""
mutation Save($input: CustomerInput!) {{
  saveCustomer(input: $input) {{ customer {{ id name taxId emails city country }} {ERRORS} }}
}}
"""


def test_save_customer_creates_a_customer_for_the_token_tenant() -> None:
    """Without an id a customer is created in the tenant of the token."""
    tenant, token = make_world("A", READ_WRITE)
    data = run(
        SAVE_CUSTOMER,
        token,
        input={"name": "Acme", "taxId": "12-3456789", "emails": ["ap@acme.test"], "city": "Austin"},
    )["saveCustomer"]
    assert data["errors"] == []
    assert data["customer"]["name"] == "Acme"
    assert data["customer"]["emails"] == ["ap@acme.test"]
    assert data["customer"]["country"] == "US"
    with tenant_context(tenant.pk):
        saved = Customer.objects.get(name="Acme")
    assert (saved.tax_id, saved.city, str(saved.pk)) == (
        "12-3456789",
        "Austin",
        data["customer"]["id"],
    )


def test_save_customer_with_an_id_replaces_that_customer() -> None:
    """An id selects an existing customer, whose fields are replaced by the input."""
    tenant, token = make_world("A", READ_WRITE)
    with tenant_context(tenant.pk):
        existing = Customer.objects.get()
    data = run(SAVE_CUSTOMER, token, input={"id": str(existing.pk), "name": "Renamed"})[
        "saveCustomer"
    ]
    assert data["errors"] == []
    with tenant_context(tenant.pk):
        assert Customer.objects.count() == 1
        assert Customer.objects.get().name == "Renamed"


def test_save_customer_reports_validation_errors_in_the_payload() -> None:
    """Invalid data gives a null customer and one VALIDATION_ERROR per field and message."""
    tenant, token = make_world("A", READ_WRITE)
    data = run(SAVE_CUSTOMER, token, input={"name": "", "emails": ["not-an-email"]})["saveCustomer"]
    assert data["customer"] is None
    fields = {(e["code"], e["field"]) for e in data["errors"]}
    assert fields == {("VALIDATION_ERROR", "name"), ("VALIDATION_ERROR", "emails")}
    with tenant_context(tenant.pk):
        assert Customer.objects.count() == 1


def test_save_customer_without_the_write_scope_reports_permission_denied() -> None:
    """A read-only token gets a PERMISSION_DENIED entry, not a GraphQL error."""
    _, token = make_world("A", ["invoices:read"])
    data = run(SAVE_CUSTOMER, token, input={"name": "Acme"})["saveCustomer"]
    assert data["customer"] is None
    assert [e["code"] for e in data["errors"]] == ["PERMISSION_DENIED"]
    assert "invoices:write" in data["errors"][0]["message"]


def test_save_customer_with_an_id_needs_the_read_scope_too() -> None:
    """Looking up the id goes through the read service, so a write-only token is refused."""
    tenant, token = make_world("A", ["invoices:write"])
    with tenant_context(tenant.pk):
        existing = Customer.objects.get()
    data = run(SAVE_CUSTOMER, token, input={"id": str(existing.pk), "name": "Renamed"})[
        "saveCustomer"
    ]
    assert [e["code"] for e in data["errors"]] == ["PERMISSION_DENIED"]
    assert "invoices:read" in data["errors"][0]["message"]


def test_save_customer_with_an_unknown_or_malformed_id() -> None:
    """A missing id is NOT_FOUND and a non-numeric id is a validation error on id."""
    _, token = make_world("A", READ_WRITE)
    missing = run(SAVE_CUSTOMER, token, input={"id": "999999", "name": "X"})["saveCustomer"]
    assert [(e["code"], e["field"]) for e in missing["errors"]] == [("NOT_FOUND", "id")]
    bad = run(SAVE_CUSTOMER, token, input={"id": "abc", "name": "X"})["saveCustomer"]
    assert [(e["code"], e["field"]) for e in bad["errors"]] == [("VALIDATION_ERROR", "id")]


def catalog(tenant: Tenant) -> tuple[Customer, Product, TaxRate]:
    """Create a customer, a product and a tax rate in a tenant.

    Args:
        tenant: Tenant to create them in.

    Returns:
        The customer, the product and the 10 percent tax rate.
    """
    with tenant_context(tenant.pk):
        return (
            Customer.objects.create(name=f"{tenant.name} buyer"),
            Product.objects.create(name=f"{tenant.name} widget", unit_price=1000),
            TaxRate.objects.create(name=f"{tenant.name} VAT", rate_bp=1000),
        )


SAVE_DRAFT = f"""
mutation Save($input: DraftInvoiceInput!) {{
  saveDraft(input: $input) {{
    invoice {{
      id status currency subtotal taxTotal total dueDate notes
      lines {{
        totalCount
        results {{ position description quantity unitPrice discountBp net total }}
      }}
    }}
    {ERRORS}
  }}
}}
"""


def draft_input(customer: Customer, **changes: Any) -> dict[str, Any]:
    """Build a saveDraft input with two lines, the second one taxed.

    Args:
        customer: Customer to invoice.
        **changes: Members to add to or replace in the input.

    Returns:
        The input value.
    """
    base: dict[str, Any] = {
        "customerId": str(customer.pk),
        "lines": [
            {"description": "Setup", "quantity": "2.5", "unitPrice": "1000"},
            {"description": "Support", "quantity": "1", "unitPrice": "4000", "discountBp": 500},
        ],
    }
    return {**base, **changes}


def test_save_draft_creates_a_draft_with_calculated_totals() -> None:
    """A new draft is stored with its lines, taxes from tax rates, and totals in cents."""
    tenant, token = make_world("A", READ_WRITE)
    customer, product, rate = catalog(tenant)
    lines = [
        {
            "description": "Widget",
            "quantity": "2.5",
            "unitPrice": "1000",
            "productId": str(product.pk),
            "taxRateIds": [str(rate.pk)],
        },
        {"description": "Support", "quantity": "1", "unitPrice": "4000", "discountBp": 500},
    ]
    data = run(SAVE_DRAFT, token, input=draft_input(customer, lines=lines, notes="Thanks"))[
        "saveDraft"
    ]
    assert data["errors"] == []
    invoice = data["invoice"]
    assert (invoice["status"], invoice["currency"], invoice["notes"]) == ("DRAFT", "USD", "Thanks")
    # 2.5 x 1000 = 2500 taxed at 10 percent, plus 4000 less 5 percent = 3800 untaxed.
    assert (invoice["subtotal"], invoice["taxTotal"], invoice["total"]) == ("6300", "250", "6550")
    assert invoice["lines"]["totalCount"] == 2
    assert [row["description"] for row in invoice["lines"]["results"]] == ["Widget", "Support"]
    with tenant_context(tenant.pk):
        stored = Invoice.objects.get(pk=invoice["id"])
        assert stored.lines.get(position=1).taxes.get().rate_bp == 1000
        assert stored.lines.get(position=1).product == product


def test_save_draft_takes_the_currency_and_due_date() -> None:
    """A currency and a due date in the input are stored on the draft."""
    tenant, token = make_world("A", READ_WRITE)
    customer, _, _ = catalog(tenant)
    data = run(
        SAVE_DRAFT, token, input=draft_input(customer, currency="CAD", dueDate="2026-12-31")
    )["saveDraft"]
    assert (data["invoice"]["currency"], data["invoice"]["dueDate"]) == ("CAD", "2026-12-31")


def test_save_draft_with_an_id_replaces_the_lines_of_that_draft() -> None:
    """Editing keeps the invoice, replaces its lines, and keeps the currency when omitted."""
    tenant, token = make_world("A", READ_WRITE)
    customer, _, _ = catalog(tenant)
    created = run(SAVE_DRAFT, token, input=draft_input(customer, currency="CAD"))["saveDraft"]
    edit = draft_input(
        customer,
        id=created["invoice"]["id"],
        lines=[{"description": "Only", "quantity": "1", "unitPrice": "100"}],
    )
    data = run(SAVE_DRAFT, token, input=edit)["saveDraft"]
    assert data["errors"] == []
    assert data["invoice"]["id"] == created["invoice"]["id"]
    assert data["invoice"]["currency"] == "CAD"
    assert data["invoice"]["total"] == "100"
    with tenant_context(tenant.pk):
        assert InvoiceLine.objects.filter(invoice_id=created["invoice"]["id"]).count() == 1


def test_save_draft_refuses_an_invoice_that_is_not_a_draft() -> None:
    """Editing an issued invoice is an INVALID_TRANSITION entry."""
    tenant, token = make_world("A", READ_WRITE)
    with tenant_context(tenant.pk):
        issued = Invoice.objects.filter(status=Invoice.Status.ISSUED).get()
        customer = issued.customer
    data = run(SAVE_DRAFT, token, input=draft_input(customer, id=str(issued.pk)))["saveDraft"]
    assert data["invoice"] is None
    assert [e["code"] for e in data["errors"]] == ["INVALID_TRANSITION"]


def test_save_draft_reports_each_bad_input_value_with_its_path() -> None:
    """Pydantic failures name the camelCase path of the value, list positions included."""
    tenant, token = make_world("A", READ_WRITE)
    customer, _, _ = catalog(tenant)
    lines = [
        {"description": "ok", "quantity": "1", "unitPrice": "1"},
        {"description": "bad", "quantity": "1.2345", "unitPrice": "1", "discountBp": 20000},
    ]
    data = run(SAVE_DRAFT, token, input=draft_input(customer, lines=lines))["saveDraft"]
    assert data["invoice"] is None
    assert {(e["code"], e["field"]) for e in data["errors"]} == {
        ("VALIDATION_ERROR", "lines[1].quantity"),
        ("VALIDATION_ERROR", "lines[1].discountBp"),
    }
    with tenant_context(tenant.pk):
        assert not Invoice.objects.filter(status=Invoice.Status.DRAFT).exists()


def test_save_draft_reports_unknown_ids_as_not_found_with_their_path() -> None:
    """A customer, product or tax rate that is not visible is NOT_FOUND at its own path."""
    tenant, token = make_world("A", READ_WRITE)
    customer, _, rate = catalog(tenant)
    missing_customer = run(SAVE_DRAFT, token, input=draft_input(customer, customerId="999999"))[
        "saveDraft"
    ]
    assert [(e["code"], e["field"]) for e in missing_customer["errors"]] == [
        ("NOT_FOUND", "customerId")
    ]
    product_lines = [
        {"description": "a", "quantity": "1", "unitPrice": "1", "productId": "999999"},
    ]
    missing_product = run(SAVE_DRAFT, token, input=draft_input(customer, lines=product_lines))[
        "saveDraft"
    ]
    assert [(e["code"], e["field"]) for e in missing_product["errors"]] == [
        ("NOT_FOUND", "lines[0].productId")
    ]
    rate_lines = [
        {"description": "a", "quantity": "1", "unitPrice": "1"},
        {
            "description": "b",
            "quantity": "1",
            "unitPrice": "1",
            "taxRateIds": [str(rate.pk), "999999"],
        },
    ]
    missing_rate = run(SAVE_DRAFT, token, input=draft_input(customer, lines=rate_lines))[
        "saveDraft"
    ]
    assert [(e["code"], e["field"]) for e in missing_rate["errors"]] == [
        ("NOT_FOUND", "lines[1].taxRateIds")
    ]


def test_save_draft_needs_both_scopes() -> None:
    """Without invoices:read the lookups are refused, and without invoices:write the save is."""
    tenant, token = make_world("A", ["invoices:write"])
    customer, _, _ = catalog(tenant)
    write_only = run(SAVE_DRAFT, token, input=draft_input(customer))["saveDraft"]
    assert [e["code"] for e in write_only["errors"]] == ["PERMISSION_DENIED"]
    assert "invoices:read" in write_only["errors"][0]["message"]
    tenant_b, token_b = make_world("B", ["invoices:read"])
    customer_b, _, _ = catalog(tenant_b)
    read_only = run(SAVE_DRAFT, token_b, input=draft_input(customer_b))["saveDraft"]
    assert [e["code"] for e in read_only["errors"]] == ["PERMISSION_DENIED"]
    assert "invoices:write" in read_only["errors"][0]["message"]


def test_a_failed_mutation_writes_nothing_and_the_request_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure after a partial write is rolled back, and a later mutation still succeeds.

    Args:
        monkeypatch: Fixture that replaces the save_draft service.
    """
    from django.core.exceptions import ValidationError

    tenant, token = make_world("A", READ_WRITE)
    customer, _, _ = catalog(tenant)

    def write_then_fail(principal: Any, invoice: Invoice, lines: Any) -> Invoice:
        invoice.currency = Currency.USD
        invoice.save()
        raise ValidationError("late failure")

    monkeypatch.setattr("api.mutations.save_draft", write_then_fail)
    query = f"""
    mutation Both($draft: DraftInvoiceInput!, $customer: CustomerInput!) {{
      first: saveDraft(input: $draft) {{ invoice {{ id }} {ERRORS} }}
      second: saveCustomer(input: $customer) {{ customer {{ name }} {ERRORS} }}
    }}
    """
    data = run(query, token, draft=draft_input(customer), customer={"name": "Still works"})
    assert data["first"]["invoice"] is None
    assert data["first"]["errors"] == [
        {"code": "VALIDATION_ERROR", "message": "late failure", "field": None}
    ]
    assert data["second"]["customer"] == {"name": "Still works"}
    with tenant_context(tenant.pk):
        assert not Invoice.objects.filter(status=Invoice.Status.DRAFT).exists()
        assert Customer.objects.filter(name="Still works").exists()


def test_a_mutation_leaves_an_audit_event_whose_actor_is_the_token() -> None:
    """The service records the event with the token as the actor."""
    tenant, token = make_world("A", READ_WRITE)
    customer, _, _ = catalog(tenant)
    invoice_id = run(SAVE_DRAFT, token, input=draft_input(customer))["saveDraft"]["invoice"]["id"]
    with tenant_context(tenant.pk):
        event = AuditEvent.objects.get(entity_id=invoice_id)
        assert event.action == "invoice.created"
        assert event.actor_kind == AuditEvent.ActorKind.TOKEN
        assert event.actor_token == ApiToken.objects.get()


DELETE_DRAFT = f"""
mutation Delete($id: ID!) {{ deleteDraft(id: $id) {{ id {ERRORS} }} }}
"""


def make_draft(tenant: Tenant, token: str) -> str:
    """Save a draft through the API.

    Args:
        tenant: Tenant of the token.
        token: Plaintext API token with both scopes.

    Returns:
        The id of the draft.
    """
    customer, _, _ = catalog(tenant)
    data = run(SAVE_DRAFT, token, input=draft_input(customer))["saveDraft"]
    return str(data["invoice"]["id"])


def test_delete_draft_removes_the_draft_and_returns_its_id() -> None:
    """The draft and its lines are gone, and the audit log keeps the deletion."""
    tenant, token = make_world("A", READ_WRITE)
    draft_id = make_draft(tenant, token)
    data = run(DELETE_DRAFT, token, id=draft_id)["deleteDraft"]
    assert data == {"id": draft_id, "errors": []}
    with tenant_context(tenant.pk):
        assert not Invoice.objects.filter(pk=draft_id).exists()
        assert AuditEvent.objects.filter(entity_id=draft_id, action="invoice.deleted").exists()


def test_delete_draft_refuses_an_invoice_that_is_not_a_draft() -> None:
    """An issued invoice is an INVALID_TRANSITION entry and stays."""
    tenant, token = make_world("A", READ_WRITE)
    with tenant_context(tenant.pk):
        issued = Invoice.objects.filter(status=Invoice.Status.ISSUED).get()
    data = run(DELETE_DRAFT, token, id=str(issued.pk))["deleteDraft"]
    assert data["id"] is None
    assert [e["code"] for e in data["errors"]] == ["INVALID_TRANSITION"]
    with tenant_context(tenant.pk):
        assert Invoice.objects.filter(pk=issued.pk).exists()


def test_delete_draft_reports_unknown_malformed_and_unreadable_ids() -> None:
    """A missing id is NOT_FOUND, a malformed one a validation error, a write-only token denied."""
    tenant, token = make_world("A", READ_WRITE)
    draft_id = make_draft(tenant, token)
    missing = run(DELETE_DRAFT, token, id="00000000-0000-0000-0000-000000000000")["deleteDraft"]
    assert [(e["code"], e["field"]) for e in missing["errors"]] == [("NOT_FOUND", "id")]
    bad = run(DELETE_DRAFT, token, id="abc")["deleteDraft"]
    assert [(e["code"], e["field"]) for e in bad["errors"]] == [("VALIDATION_ERROR", "id")]
    tenant_w, token_w = make_world("W", ["invoices:write"])
    with tenant_context(tenant_w.pk):
        other = Invoice.objects.filter(status=Invoice.Status.ISSUED).get()
    denied = run(DELETE_DRAFT, token_w, id=str(other.pk))["deleteDraft"]
    assert [e["code"] for e in denied["errors"]] == ["PERMISSION_DENIED"]
    with tenant_context(tenant.pk):
        assert Invoice.objects.filter(pk=draft_id).exists()


def test_an_unexpected_exception_is_a_graphql_error_not_a_payload_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bug in a service is masked by graphex as a top-level error and never typed.

    Args:
        monkeypatch: Fixture that replaces the save_customer service.
    """

    def explode(principal: Any, customer: Customer) -> Customer:
        raise RuntimeError("secret detail")

    monkeypatch.setattr("api.mutations.save_customer", explode)
    _, token = make_world("A", READ_WRITE)
    body = post(SAVE_CUSTOMER, token, input={"name": "Acme"}).json()
    assert body["data"]["saveCustomer"] is None
    assert "secret detail" not in str(body)
    assert body["errors"]
