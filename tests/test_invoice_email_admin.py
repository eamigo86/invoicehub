"""The admin action that emails issued invoices."""

from decimal import Decimal

import pytest
from django.test import Client

from accounts.models import User
from billing.models import AuditEvent, Customer, Invoice, InvoiceEmail
from billing.services import DraftLine, issue_invoice, save_draft
from tenancy.context import tenant_context
from tenancy.models import Membership, Tenant
from tenancy.permissions import Permission, Principal

INVOICES_URL = "/admin/billing/invoice/"
LABEL = "Email selected invoices"


@pytest.fixture
def tenant() -> Tenant:
    """Create a tenant with complete issuer details.

    Returns:
        The tenant.
    """
    return Tenant.objects.create(
        name="A",
        legal_name="Acme Inc.",
        address_line1="1 Main St",
        city="Austin",
        state="TX",
        postal_code="78701",
    )


def login(client: Client, tenant: Tenant, role: Membership.Role) -> None:
    """Log in a superuser holding a role in the tenant and make the tenant active.

    Args:
        client: Test client to prepare.
        tenant: Tenant to activate.
        role: Role of the membership.
    """
    user = User.objects.create_superuser("root", "root@example.com", "pw")
    Membership.objects.create(user=user, tenant=tenant, role=role)
    client.force_login(user)
    session = client.session
    session["tenant_id"] = str(tenant.pk)
    session.save()


def make_documents(tenant: Tenant) -> tuple[Invoice, Invoice]:
    """Create one issued invoice and one draft, in committed contexts.

    Args:
        tenant: Tenant that owns them.

    Returns:
        The issued invoice and the draft.
    """
    principal = Principal(tenant.pk, frozenset({Permission.INVOICES_WRITE}))
    lines = [DraftLine("A", Decimal(1), 500)]
    with tenant_context(tenant.pk):
        customer = Customer.objects.create(name="Bolt", emails=["ap@bolt.example"])
        issued = issue_invoice(principal, save_draft(principal, Invoice(customer=customer), lines))
        draft = save_draft(principal, Invoice(customer=customer), lines)
    return issued, draft


@pytest.mark.django_db(transaction=True)
def test_the_action_requests_the_email_and_explains_the_refusal(
    tenant: Tenant, client: Client
) -> None:
    """An issued invoice gets a request with a fresh key and a draft is reported as refused.

    Args:
        tenant: Tenant that owns the documents.
        client: Test client.
    """
    login(client, tenant, Membership.Role.ACCOUNTANT)
    issued, draft = make_documents(tenant)
    assert LABEL in client.get(INVOICES_URL).content.decode()
    selected = [str(draft.pk), str(issued.pk)]
    for _ in range(2):
        response = client.post(
            INVOICES_URL,
            {"action": "email_selected_invoices", "_selected_action": selected},
            follow=True,
        )
    messages = [str(m) for m in response.context["messages"]]
    assert any(m.startswith("Email requested") and issued.reference in m for m in messages)
    assert any(m.startswith(f"{draft.pk}:") and "draft" in m for m in messages)
    with tenant_context(tenant.pk):
        keys = set(InvoiceEmail.objects.values_list("idempotency_key", flat=True))
        assert len(keys) == 2
        assert AuditEvent.objects.filter(action="invoice.send_requested").count() == 2


@pytest.mark.django_db(transaction=True)
def test_a_viewer_has_no_email_action(tenant: Tenant, client: Client) -> None:
    """Without invoices:write the action is not offered and nothing is requested.

    Args:
        tenant: Tenant that owns the documents.
        client: Test client.
    """
    login(client, tenant, Membership.Role.VIEWER)
    issued, _ = make_documents(tenant)
    assert LABEL not in client.get(INVOICES_URL).content.decode()
    client.post(
        INVOICES_URL, {"action": "email_selected_invoices", "_selected_action": [str(issued.pk)]}
    )
    with tenant_context(tenant.pk):
        assert not InvoiceEmail.objects.exists()
