"""A Bearer token pins the request to its tenant and never makes it a user."""

import pytest
from django.test import Client

from accounts.models import User
from billing.models import Customer
from tenancy.context import tenant_context
from tenancy.models import Membership, Tenant
from tenancy.permissions import Principal
from tenancy.tokens import issue_token, revoke_token

pytestmark = [
    pytest.mark.isolation,
    pytest.mark.django_db(transaction=True),
    pytest.mark.urls("tests.urls_tenant_probe"),
]


def tenant_with_token(name: str) -> tuple[Tenant, Principal, str]:
    """Create a tenant owning one customer, with an admin and a token.

    Args:
        name: Tenant name, also the customer name.

    Returns:
        The tenant, its admin principal and a plaintext token for it.
    """
    tenant = Tenant.objects.create(name=name)
    with tenant_context(tenant.pk):
        Customer.objects.create(name=f"customer of {name}")
    user = User.objects.create_user(f"owner-{name}", f"owner-{name}@example.com", "pw")
    membership = Membership.objects.create(user=user, tenant=tenant, role=Membership.Role.OWNER)
    principal = Principal.from_membership(membership)
    _, plaintext = issue_token(principal, "ci", ["invoices:read"])
    return tenant, principal, plaintext


def test_a_token_of_a_sees_only_the_customers_of_a() -> None:
    """The token tenant is the only tenant the view can read, and it is not a user."""
    tenant_with_token("B")
    _, _, plaintext = tenant_with_token("A")
    response = Client().get("/customers/", headers={"Authorization": f"Bearer {plaintext}"})
    assert response.content.decode().splitlines() == ["authenticated=False", "customer of A"]


def test_a_revoked_token_gets_401() -> None:
    """Revocation takes effect on the next request."""
    tenant, principal, plaintext = tenant_with_token("A")
    revoke_token(principal, tenant.api_tokens.get().pk)
    response = Client().get("/customers/", headers={"Authorization": f"Bearer {plaintext}"})
    assert response.status_code == 401
    assert response["WWW-Authenticate"] == 'Bearer error="invalid_token"'


def test_a_token_of_a_beats_a_session_naming_b_and_leaves_the_session_untouched() -> None:
    """A member of B holding A's token still reads only A, and keeps the B session."""
    b, _, _ = tenant_with_token("B")
    _, _, plaintext = tenant_with_token("A")
    member = User.objects.get(username="owner-B")
    client = Client()
    client.force_login(member)
    session = client.session
    session["tenant_id"] = str(b.pk)
    session.save()
    response = client.get("/customers/", headers={"Authorization": f"Bearer {plaintext}"})
    assert response.content.decode().splitlines() == ["authenticated=False", "customer of A"]
    assert client.session["tenant_id"] == str(b.pk)
