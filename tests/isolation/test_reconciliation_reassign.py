"""The reassign form of the proposal admin only reaches the active tenant's rows."""

import pytest
from django.test import Client

from accounts.models import User
from reconciliation.models import MatchProposal
from tenancy.models import Membership, Tenant
from tests.test_payments import make_tenant
from tests.test_reconciliation_review_admin import BASE, seed, seed_invoice, state_of

pytestmark = [pytest.mark.isolation, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def two_tenants(client: Client) -> tuple[Tenant, Tenant]:
    """Create tenants A and B and log in as an owner of A with A active.

    Args:
        client: Django test client fixture.

    Returns:
        Tenant A and tenant B.
    """
    a = make_tenant()
    b = make_tenant()
    user = User.objects.create_superuser("root", "root@example.com", "pw")
    Membership.objects.create(user=user, tenant=a, role=Membership.Role.OWNER)
    client.force_login(user)
    session = client.session
    session["tenant_id"] = str(a.pk)
    session.save()
    return a, b


def test_tenant_a_cannot_open_or_post_the_reassign_form_of_b(
    client: Client, two_tenants: tuple[Tenant, Tenant]
) -> None:
    """B's proposal answers 404 to the form and to a post, and stays pending.

    Args:
        client: Django test client fixture.
        two_tenants: Tenants A and B, logged in as A.
    """
    a, b = two_tenants
    foreign = seed(b)
    mine = seed_invoice(a)
    url = f"{BASE}/matchproposal/{foreign.pk}/reassign/"
    assert client.get(url).status_code == 404
    assert client.post(url, {"invoices": [str(mine.pk)]}).status_code == 404
    assert state_of(b, foreign) == MatchProposal.State.PENDING


def test_tenant_a_cannot_pick_the_invoices_of_b(
    client: Client, two_tenants: tuple[Tenant, Tenant]
) -> None:
    """B's invoice is not offered and a post naming it is refused as an invalid choice.

    Args:
        client: Django test client fixture.
        two_tenants: Tenants A and B, logged in as A.
    """
    a, b = two_tenants
    proposal = seed(a)
    foreign = seed_invoice(b)
    url = f"{BASE}/matchproposal/{proposal.pk}/reassign/"
    assert str(foreign.pk) not in client.get(url).content.decode()
    response = client.post(url, {"invoices": [str(foreign.pk)]})
    assert response.status_code == 200
    assert "valid choice" in response.content.decode()
    assert state_of(a, proposal) == MatchProposal.State.PENDING
