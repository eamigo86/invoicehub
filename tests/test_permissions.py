"""The permission service: role matrix, principals and checks."""

import uuid

import pytest
from django.core.exceptions import PermissionDenied

from accounts.models import User
from tenancy import permissions
from tenancy.context import tenant_context
from tenancy.models import Membership, Tenant


def test_permission_values_are_the_documented_scopes() -> None:
    """The permission values are exactly the four documented strings."""
    assert [p.value for p in permissions.Permission] == [
        "invoices:read",
        "invoices:write",
        "reconciliation:write",
        "tokens:manage",
    ]


def test_every_role_has_an_entry_in_the_matrix() -> None:
    """No role is left without a permission set."""
    assert set(permissions.ROLE_PERMISSIONS) == set(Membership.Role)


def test_the_role_matrix() -> None:
    """Owner and admin hold all, accountant everything but tokens, viewer read only."""
    everything = frozenset(permissions.Permission)
    read = permissions.Permission.INVOICES_READ
    assert permissions.ROLE_PERMISSIONS[Membership.Role.OWNER] == everything
    assert permissions.ROLE_PERMISSIONS[Membership.Role.ADMIN] == everything
    assert permissions.ROLE_PERMISSIONS[Membership.Role.ACCOUNTANT] == everything - {
        permissions.Permission.TOKENS_MANAGE
    }
    assert permissions.ROLE_PERMISSIONS[Membership.Role.VIEWER] == {read}


@pytest.mark.django_db
def test_a_principal_from_a_membership_carries_tenant_and_role_permissions() -> None:
    """The principal copies the tenant id and the permissions of the role."""
    user = User.objects.create_user("ann", "ann@example.com", "pw")
    tenant = Tenant.objects.create(name="Acme")
    membership = Membership.objects.create(user=user, tenant=tenant, role=Membership.Role.VIEWER)
    principal = permissions.Principal.from_membership(membership)
    assert principal.tenant_id == tenant.pk
    assert principal.permissions == {permissions.Permission.INVOICES_READ}


def test_a_principal_is_immutable() -> None:
    """Assigning to a principal field raises."""
    principal = permissions.Principal(uuid.uuid4(), frozenset())
    with pytest.raises(AttributeError):
        principal.tenant_id = uuid.uuid4()  # type: ignore[misc]


def test_has_permission_checks_membership_of_the_set_and_refuses_none() -> None:
    """True only when the principal holds the permission; no principal means False."""
    read = permissions.Permission.INVOICES_READ
    write = permissions.Permission.INVOICES_WRITE
    principal = permissions.Principal(uuid.uuid4(), frozenset({read}))
    assert permissions.has_permission(principal, read) is True
    assert permissions.has_permission(principal, write) is False
    assert permissions.has_permission(None, read) is False


def test_require_permission_raises_permission_denied_unless_held() -> None:
    """Missing permission or no principal raises; a held permission passes."""
    read = permissions.Permission.INVOICES_READ
    write = permissions.Permission.INVOICES_WRITE
    principal = permissions.Principal(uuid.uuid4(), frozenset({read}))
    permissions.require_permission(principal, read)
    with pytest.raises(PermissionDenied):
        permissions.require_permission(principal, write)
    with pytest.raises(PermissionDenied):
        permissions.require_permission(None, read)


@pytest.mark.django_db
def test_require_permission_refuses_a_principal_of_another_active_tenant() -> None:
    """A principal holding the permission is still refused while another tenant is active."""
    write = permissions.Permission.INVOICES_WRITE
    a, b = Tenant.objects.create(name="A"), Tenant.objects.create(name="B")
    principal = permissions.Principal(a.pk, frozenset({write}))
    with tenant_context(a.pk):
        permissions.require_permission(principal, write)
    with tenant_context(b.pk), pytest.raises(PermissionDenied):
        permissions.require_permission(principal, write)
