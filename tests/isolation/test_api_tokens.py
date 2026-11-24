"""API tokens never cross tenants and are never stored in the clear."""

import pytest
from django.core.exceptions import PermissionDenied
from django.http import Http404

from accounts.models import User
from tenancy.models import ApiToken, Membership, Tenant
from tenancy.permissions import Principal
from tenancy.tokens import issue_token, revoke_token

pytestmark = [pytest.mark.isolation, pytest.mark.django_db]


def principal_of(tenant: Tenant, role: Membership.Role) -> Principal:
    """Create a user holding a role in a tenant and return its principal.

    Args:
        tenant: Tenant to join.
        role: Role of the membership.

    Returns:
        The principal of the new member.
    """
    name = f"{role}-{tenant.name}"
    user = User.objects.create_user(name, f"{name}@example.com", "pw")
    return Principal.from_membership(Membership.objects.create(user=user, tenant=tenant, role=role))


def test_a_principal_of_a_cannot_revoke_a_token_of_b() -> None:
    """The answer is the same 404 as for a missing token and B's token stays active."""
    a, b = Tenant.objects.create(name="A"), Tenant.objects.create(name="B")
    token_b, _ = issue_token(principal_of(b, Membership.Role.OWNER), "ci", ["invoices:read"])
    with pytest.raises(Http404):
        revoke_token(principal_of(a, Membership.Role.OWNER), token_b.pk)
    token_b.refresh_from_db()
    assert token_b.revoked_at is None


@pytest.mark.parametrize("role", [Membership.Role.ACCOUNTANT, Membership.Role.VIEWER])
def test_issuing_is_refused_without_tokens_manage(role: Membership.Role) -> None:
    """Accountants and viewers cannot issue tokens.

    Args:
        role: Role without tokens:manage.
    """
    tenant = Tenant.objects.create(name="A")
    with pytest.raises(PermissionDenied):
        issue_token(principal_of(tenant, role), "ci", ["invoices:read"])
    assert not ApiToken.objects.exists()


def test_the_stored_row_never_contains_the_plaintext() -> None:
    """No column of the stored row holds the plaintext token."""
    tenant = Tenant.objects.create(name="A")
    token, plaintext = issue_token(
        principal_of(tenant, Membership.Role.OWNER), "ci", ["invoices:read"]
    )
    row = ApiToken.objects.filter(pk=token.pk).values().get()
    assert not any(plaintext in str(value) for value in row.values())
