"""Tenancy services: company creation, membership listing and tenant selection."""

import uuid

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.http import Http404

from accounts.models import User
from tenancy.models import Membership, Tenant
from tenancy.services import (
    create_tenant,
    list_memberships,
    select_single_tenant,
    select_tenant,
)

pytestmark = pytest.mark.django_db


def make_user(name: str = "ann") -> User:
    """Create a user.

    Args:
        name: Username, also used for the email address.

    Returns:
        The stored user.
    """
    return User.objects.create_user(name, f"{name}@example.com", "pw")


def join(user: User, name: str, role: Membership.Role = Membership.Role.VIEWER) -> Tenant:
    """Create a tenant and add the user to it.

    Args:
        user: User who becomes a member.
        name: Tenant name.
        role: Role of the membership.

    Returns:
        The new tenant.
    """
    tenant = Tenant.objects.create(name=name)
    Membership.objects.create(user=user, tenant=tenant, role=role)
    return tenant


def test_create_tenant_stores_it_and_makes_the_creator_its_owner() -> None:
    """The tenant exists and its only member is the creator, as owner."""
    user = make_user()
    tenant = create_tenant(user, "Acme")
    assert list(Tenant.objects.filter(pk=tenant.pk).values_list("name", flat=True)) == ["Acme"]
    assert list(tenant.memberships.values_list("user_id", "role")) == [(user.pk, "owner")]


def test_create_tenant_is_all_or_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the membership cannot be saved, no tenant is left behind.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """

    def fail(*args: object, **kwargs: object) -> None:
        """Refuse to create a membership.

        Args:
            *args: Ignored.
            **kwargs: Ignored.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("boom")

    monkeypatch.setattr(Membership.objects, "create", fail)
    with pytest.raises(RuntimeError):
        create_tenant(make_user(), "Acme")
    assert Tenant.objects.count() == 0


def test_list_memberships_returns_only_the_users_own_ordered_by_tenant_name() -> None:
    """Another user's memberships never appear, and tenants come in name order."""
    ann, bob = make_user("ann"), make_user("bob")
    join(ann, "Zeta")
    join(ann, "Alpha")
    join(bob, "Other")
    names = [m.tenant.name for m in list_memberships(ann)]
    assert names == ["Alpha", "Zeta"]


def test_select_tenant_stores_the_tenant_for_a_member() -> None:
    """A member's session receives the tenant id and the membership is returned."""
    user = make_user()
    tenant = join(user, "Acme", Membership.Role.OWNER)
    session = SessionStore()
    membership = select_tenant(session, user, tenant.pk)
    assert session.get("tenant_id") == str(tenant.pk)
    assert membership.role == "owner"


@pytest.mark.parametrize("known", [True, False], ids=["foreign", "unknown"])
def test_select_tenant_refuses_non_members_the_same_way(known: bool) -> None:
    """A foreign tenant and a missing one both raise Http404 and leave the session alone.

    Args:
        known: Whether the tenant exists but belongs to someone else.
    """
    user = make_user()
    mine = join(user, "Mine")
    other = join(make_user("bob"), "Theirs")
    session = SessionStore()
    session["tenant_id"] = str(mine.pk)
    with pytest.raises(Http404):
        select_tenant(session, user, other.pk if known else uuid.uuid4())
    assert session.get("tenant_id") == str(mine.pk)


def test_select_single_tenant_selects_the_only_membership() -> None:
    """With exactly one membership the tenant is selected."""
    user = make_user()
    tenant = join(user, "Acme")
    session = SessionStore()
    select_single_tenant(session, user)
    assert session.get("tenant_id") == str(tenant.pk)


@pytest.mark.parametrize("count", [0, 2])
def test_select_single_tenant_selects_nothing_otherwise(count: int) -> None:
    """With no membership or several, the session is left without a tenant.

    Args:
        count: Number of memberships of the user.
    """
    user = make_user()
    for index in range(count):
        join(user, f"T{index}")
    session = SessionStore()
    select_single_tenant(session, user)
    assert "tenant_id" not in session
