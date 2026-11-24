"""API tokens: scopes, issuing, revoking and authenticating."""

import hashlib

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404

from accounts.models import User
from tenancy.models import ApiToken, Membership, Tenant
from tenancy.permissions import TOKEN_SCOPES, Permission, Principal
from tenancy.tokens import authenticate_token, issue_token, revoke_token

pytestmark = pytest.mark.django_db

READ = Permission.INVOICES_READ.value


def admin_of(name: str = "A") -> tuple[Principal, Tenant]:
    """Create a tenant and the principal of one of its admins.

    Args:
        name: Tenant name.

    Returns:
        The admin principal and the tenant.
    """
    tenant = Tenant.objects.create(name=name)
    user = User.objects.create_user(f"admin-{name}", f"admin-{name}@example.com", "pw")
    membership = Membership.objects.create(user=user, tenant=tenant, role=Membership.Role.ADMIN)
    return Principal.from_membership(membership), tenant


def test_token_scopes_are_the_rfc_scopes_and_never_tokens_manage() -> None:
    """Tokens may carry the three RFC 0002 scopes and nothing else."""
    assert sorted(scope.value for scope in TOKEN_SCOPES) == [
        "invoices:read",
        "invoices:write",
        "reconciliation:write",
    ]
    assert Permission.TOKENS_MANAGE not in TOKEN_SCOPES


def test_a_token_principal_holds_exactly_its_scopes_in_its_tenant() -> None:
    """The principal ignores any role: only the token scopes become permissions."""
    principal, tenant = admin_of()
    token, _ = issue_token(principal, "ci", [READ])
    assert Principal.from_token(token) == Principal(
        tenant.pk, frozenset({Permission.INVOICES_READ})
    )


def test_issue_returns_the_record_and_the_plaintext_once() -> None:
    """The plaintext has a recognizable prefix and only its sha256 hex is stored."""
    principal, tenant = admin_of()
    user = User.objects.get(username="admin-A")
    token, plaintext = issue_token(principal, "ci", [READ], created_by=user)
    assert plaintext.startswith("ih_")
    assert len(plaintext) > 40
    stored = ApiToken.objects.get(pk=token.pk)
    assert stored.token_hash == hashlib.sha256(plaintext.encode()).hexdigest()
    assert (stored.tenant_id, stored.name, stored.scopes) == (tenant.pk, "ci", [READ])
    assert (stored.created_by, stored.revoked_at) == (user, None)


def test_two_tokens_differ() -> None:
    """Each issue draws fresh randomness."""
    principal, _ = admin_of()
    assert issue_token(principal, "a", [READ])[1] != issue_token(principal, "b", [READ])[1]


@pytest.mark.parametrize(
    "scopes", [[], ["tokens:manage"], ["nope"], [READ, "tokens:manage"]], ids=str
)
def test_issue_rejects_empty_or_unknown_scopes(scopes: list[str]) -> None:
    """A token needs a non-empty subset of the token scopes.

    Args:
        scopes: Invalid scope list.
    """
    principal, _ = admin_of()
    with pytest.raises(ValidationError):
        issue_token(principal, "ci", scopes)
    assert not ApiToken.objects.exists()


def test_issue_rejects_a_blank_name() -> None:
    """A token needs a name."""
    principal, _ = admin_of()
    with pytest.raises(ValidationError):
        issue_token(principal, "  ", [READ])


def test_issue_deduplicates_scopes() -> None:
    """A repeated scope is stored once."""
    principal, _ = admin_of()
    token, _ = issue_token(principal, "ci", [READ, READ])
    assert token.scopes == [READ]


def test_issue_requires_a_principal_with_tokens_manage() -> None:
    """No principal and a read-only principal are refused."""
    with pytest.raises(PermissionDenied):
        issue_token(None, "ci", [READ])
    tenant = Tenant.objects.create(name="A")
    viewer = Principal(tenant.pk, frozenset({Permission.INVOICES_READ}))
    with pytest.raises(PermissionDenied):
        issue_token(viewer, "ci", [READ])


def test_revoke_stamps_the_token_and_is_idempotent() -> None:
    """Revoking twice keeps the first revocation time."""
    principal, _ = admin_of()
    token, _ = issue_token(principal, "ci", [READ])
    first = revoke_token(principal, token.pk)
    assert first.revoked_at is not None
    again = revoke_token(principal, token.pk)
    assert again.revoked_at == first.revoked_at


def test_revoke_requires_tokens_manage() -> None:
    """A principal without tokens:manage cannot revoke."""
    principal, tenant = admin_of()
    token, _ = issue_token(principal, "ci", [READ])
    viewer = Principal(tenant.pk, frozenset({Permission.INVOICES_READ}))
    with pytest.raises(PermissionDenied):
        revoke_token(viewer, token.pk)
    token.refresh_from_db()
    assert token.revoked_at is None


def test_revoke_of_an_unknown_id_is_a_404() -> None:
    """A missing token answers Http404."""
    principal, _ = admin_of()
    with pytest.raises(Http404):
        revoke_token(principal, 999999)


def test_authenticate_finds_the_active_token() -> None:
    """The plaintext leads back to its record."""
    principal, _ = admin_of()
    token, plaintext = issue_token(principal, "ci", [READ])
    assert authenticate_token(plaintext) == token


def test_authenticate_rejects_revoked_unknown_and_malformed_input() -> None:
    """Only an active, well-formed, known token authenticates."""
    principal, _ = admin_of()
    token, plaintext = issue_token(principal, "ci", [READ])
    assert authenticate_token("ih_" + "0" * 43) is None
    assert authenticate_token("") is None
    assert authenticate_token("garbage") is None
    assert authenticate_token(plaintext.removeprefix("ih_")) is None
    assert authenticate_token("ih_\ud800") is None  # not encodable as UTF-8
    revoke_token(principal, token.pk)
    assert authenticate_token(plaintext) is None


def test_a_token_is_labelled_by_its_name() -> None:
    """The string form is the token name."""
    principal, _ = admin_of()
    token, _ = issue_token(principal, "ci", [READ])
    assert str(token) == "ci"
