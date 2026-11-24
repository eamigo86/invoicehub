"""Membership: which users belong to which tenant, with what role."""

import pytest
from django.db import IntegrityError, transaction

from accounts.models import User
from tenancy.models import Membership, Tenant

pytestmark = pytest.mark.django_db


def test_roles_are_owner_admin_accountant_viewer() -> None:
    """The role choices are exactly the four documented roles."""
    assert [r.value for r in Membership.Role] == ["owner", "admin", "accountant", "viewer"]


def test_a_membership_links_a_user_to_a_tenant_with_a_role() -> None:
    """The reverse accessors list memberships and the string names user, tenant and role."""
    user = User.objects.create_user("ann", "ann@example.com", "pw")
    tenant = Tenant.objects.create(name="Acme")
    membership = Membership.objects.create(user=user, tenant=tenant, role=Membership.Role.VIEWER)
    assert list(user.memberships.all()) == [membership]  # type: ignore[attr-defined]
    assert list(tenant.memberships.all()) == [membership]
    assert str(membership) == "ann @ Acme (viewer)"
    assert membership.created_at is not None


def test_a_user_has_at_most_one_membership_per_tenant() -> None:
    """A second membership for the same user and tenant violates the unique constraint."""
    user = User.objects.create_user("ann", "ann@example.com", "pw")
    tenant = Tenant.objects.create(name="Acme")
    Membership.objects.create(user=user, tenant=tenant, role=Membership.Role.OWNER)
    with pytest.raises(IntegrityError), transaction.atomic():
        Membership.objects.create(user=user, tenant=tenant, role=Membership.Role.VIEWER)
    other = Tenant.objects.create(name="Other")
    Membership.objects.create(user=user, tenant=other, role=Membership.Role.VIEWER)
