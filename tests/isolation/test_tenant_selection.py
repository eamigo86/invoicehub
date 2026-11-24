"""Tenant selection never reveals or grants a tenant the user does not belong to."""

import uuid

import pytest
from django.test import Client

from accounts.models import User
from tenancy.models import Membership, Tenant

pytestmark = [pytest.mark.isolation, pytest.mark.django_db(transaction=True)]

PAGE = "/tenants/"


class World:
    """Two users, each owning one tenant, and a client signed in as the first.

    Attributes:
        client: Test client signed in as ann.
        ann: The signed in user.
        mine: Tenant owned by ann.
        theirs: Tenant owned by another user, whose name must never reach ann.
        bob: The other user.
    """

    def __init__(self, client: Client) -> None:
        """Create the users and tenants, and sign ann in.

        Args:
            client: Test client to sign in.
        """
        self.client = client
        self.ann = User.objects.create_user("ann", "ann@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.mine = Tenant.objects.create(name="Mine Ltd")
        self.theirs = Tenant.objects.create(name="Secret Corp")
        Membership.objects.create(user=self.ann, tenant=self.mine, role=Membership.Role.OWNER)
        Membership.objects.create(user=self.bob, tenant=self.theirs, role=Membership.Role.OWNER)
        client.force_login(self.ann)

    def select(self, tenant_id: object) -> object:
        """Post a switch request.

        Args:
            tenant_id: Identifier placed in the URL.

        Returns:
            The response.
        """
        return self.client.post(f"{PAGE}{tenant_id}/select/")


@pytest.fixture
def world(client: Client) -> World:
    """Create the two-tenant world.

    Args:
        client: Django test client fixture.

    Returns:
        The prepared world, with ann signed in and holding her only tenant.
    """
    return World(client)


def test_selecting_a_foreign_tenant_is_a_404_and_leaves_the_session_unchanged(
    world: World,
) -> None:
    """A tenant ann does not belong to cannot be selected, and her session keeps its tenant.

    Args:
        world: Two-tenant world.
    """
    assert world.client.session["tenant_id"] == str(world.mine.pk)
    response = world.select(world.theirs.pk)
    assert getattr(response, "status_code", None) == 404
    assert world.client.session["tenant_id"] == str(world.mine.pk)
    assert not Membership.objects.filter(user=world.ann, tenant=world.theirs).exists()


def test_a_foreign_tenant_and_a_missing_one_get_the_same_answer(world: World) -> None:
    """The 404 for an existing foreign tenant is indistinguishable from the one for a missing id.

    Args:
        world: Two-tenant world.
    """
    foreign = world.select(world.theirs.pk)
    missing = world.select(uuid.uuid4())
    assert getattr(foreign, "status_code", None) == getattr(missing, "status_code", None) == 404
    assert getattr(foreign, "content", b"") == getattr(missing, "content", b"")


def test_the_page_never_lists_tenants_the_user_is_not_a_member_of(world: World) -> None:
    """Only ann's own tenant is listed, even when her session names the other one.

    Args:
        world: Two-tenant world.
    """
    session = world.client.session
    session["tenant_id"] = str(world.theirs.pk)
    session.save()
    body = world.client.get(PAGE).content.decode()
    assert "Mine Ltd" in body
    assert "Secret Corp" not in body
    assert str(world.theirs.pk) not in body
    assert "tenant_id" not in world.client.session


def test_creating_a_company_makes_only_the_creator_a_member(world: World) -> None:
    """The new company has ann as its single member and other tenants stay untouched.

    Args:
        world: Two-tenant world.
    """
    response = world.client.post(PAGE, {"name": "Fresh Inc"})
    assert response.status_code == 302
    fresh = Tenant.objects.get(name="Fresh Inc")
    assert list(fresh.memberships.values_list("user_id", "role")) == [(world.ann.pk, "owner")]
    assert list(world.theirs.memberships.values_list("user_id", flat=True)) == [world.bob.pk]


def test_creating_a_company_does_not_reveal_other_tenants(world: World) -> None:
    """Neither the answer to a creation nor the page after it mentions another tenant.

    Args:
        world: Two-tenant world.
    """
    answers = [
        world.client.post(PAGE, {"name": ""}).content.decode(),
        world.client.post(PAGE, {"name": "Secret Corp"}, follow=True).content.decode(),
    ]
    assert all(str(world.theirs.pk) not in body for body in answers)
    assert not Membership.objects.filter(user=world.ann, tenant=world.theirs).exists()
    mine = list(Membership.objects.filter(user=world.ann).values_list("tenant__name", flat=True))
    assert sorted(mine) == ["Mine Ltd", "Secret Corp"]
    assert Tenant.objects.filter(name="Secret Corp").count() == 2
