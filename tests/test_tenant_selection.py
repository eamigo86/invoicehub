"""The tenant selection page, login auto-selection and the settings that point at the page."""

from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import Client, RequestFactory
from django.urls import resolve, reverse

from accounts.models import User
from core.context_processors import allauth_unfold_context
from tenancy.models import Membership, Tenant

pytestmark = pytest.mark.django_db

PAGE = "/tenants/"
PASSWORD = "S3cure-passw0rd!"
MARKER = 'data-allauth-layout="unfold"'


def make_user(name: str = "ann", *, staff: bool = False) -> User:
    """Create a user with a verified primary email address.

    Args:
        name: Username, also used for the email address.
        staff: Whether the user is a superuser.

    Returns:
        The stored user.
    """
    email = f"{name}@example.com"
    if staff:
        user = User.objects.create_superuser(name, email, PASSWORD)
    else:
        user = User.objects.create_user(name, email, PASSWORD)
    EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
    return user


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


def select_url(tenant: Tenant) -> str:
    """Build the switch URL of a tenant.

    Args:
        tenant: Tenant to switch to.

    Returns:
        The URL path.
    """
    return f"{PAGE}{tenant.pk}/select/"


def text(response: Any) -> str:
    """Decode a response body.

    Args:
        response: Response to decode.

    Returns:
        The body as text.
    """
    return str(response.content.decode())


@pytest.fixture(autouse=True)
def fresh_rate_limits() -> None:
    """Clear the cache so allauth rate limits do not leak between tests."""
    cache.clear()


def test_anonymous_visitors_are_sent_to_the_login(client: Client) -> None:
    """The page needs a signed-in user.

    Args:
        client: Django test client fixture.
    """
    response = client.get(PAGE)
    assert response.status_code == 302
    assert response.headers.get("Location") == "/accounts/login/?next=/tenants/"


def test_the_page_lists_the_users_memberships_in_the_unfold_layout(client: Client) -> None:
    """Each tenant name and role is shown in the allauth manage layout with its own switch form.

    Args:
        client: Django test client fixture.
    """
    user = make_user()
    acme = join(user, "Acme", Membership.Role.OWNER)
    join(user, "Globex", Membership.Role.VIEWER)
    client.force_login(user)
    response = client.get(PAGE)
    body = text(response)
    assert response.status_code == 200
    assert MARKER in body
    assert 'data-allauth-menu="unfold"' in body
    assert "Acme" in body
    assert "Globex" in body
    assert "Owner" in body
    assert "Viewer" in body
    assert f'action="{select_url(acme)}"' in body
    assert "csrfmiddlewaretoken" in body
    assert "Create company" in body


def test_the_page_marks_only_the_active_tenant(client: Client) -> None:
    """The active tenant carries an Active badge, and nothing is marked without one.

    Args:
        client: Django test client fixture.
    """
    user = make_user()
    acme = join(user, "Acme")
    join(user, "Globex")
    client.force_login(user)
    assert "Active" not in text(client.get(PAGE))
    client.post(select_url(acme))
    response = client.get(PAGE)
    assert response.status_code == 200
    assert response.context["active_tenant_id"] == acme.pk
    assert text(response).count("Active") == 1


def test_the_page_receives_the_unfold_site_context(client: Client) -> None:
    """The layout gets the admin site context, so the Unfold chrome has what it needs.

    Args:
        client: Django test client fixture.
    """
    client.force_login(make_user())
    response = client.get(PAGE)
    assert response.status_code == 200
    assert response.context["site_title"] == "InvoiceHub"


def test_the_context_gate_admits_the_tenancy_views_and_nothing_else() -> None:
    """The processor adds the site context for tenancy views, and not for admin or 404 pages."""
    request = RequestFactory().get(PAGE)
    request.user = AnonymousUser()
    request.resolver_match = resolve(PAGE)
    assert allauth_unfold_context(request)["site_title"] == "InvoiceHub"
    admin_request = RequestFactory().get("/admin/")
    admin_request.resolver_match = resolve("/admin/")
    assert allauth_unfold_context(admin_request) == {}


def test_switching_stores_the_tenant_and_returns_to_the_page(client: Client) -> None:
    """A POST selects the tenant and redirects back to the page.

    Args:
        client: Django test client fixture.
    """
    user = make_user()
    acme = join(user, "Acme")
    client.force_login(user)
    response = client.post(select_url(acme))
    assert response.status_code == 302
    assert response.headers.get("Location") == PAGE
    assert client.session["tenant_id"] == str(acme.pk)


def test_switching_only_accepts_post(client: Client) -> None:
    """A GET to the switch URL neither selects nor renders anything.

    Args:
        client: Django test client fixture.
    """
    user = make_user()
    acme = join(user, "Acme")
    join(user, "Globex")
    client.force_login(user)
    assert client.get(select_url(acme)).status_code == 405
    assert "tenant_id" not in client.session


def test_switching_without_a_csrf_token_is_rejected() -> None:
    """The switch form is CSRF protected."""
    user = make_user()
    acme = join(user, "Acme")
    join(user, "Globex")
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    assert client.post(select_url(acme)).status_code == 403
    assert "tenant_id" not in client.session


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/admin/", "/admin/"),
        ("https://evil.example/", PAGE),
        ("//evil.example/", PAGE),
        ("javascript:alert(1)", PAGE),
    ],
)
def test_switching_follows_only_a_safe_next_url(client: Client, target: str, expected: str) -> None:
    """The redirect honors a local next URL and ignores any other.

    Args:
        client: Django test client fixture.
        target: Value of the next field.
        expected: Where the response redirects.
    """
    user = make_user()
    acme = join(user, "Acme")
    client.force_login(user)
    response = client.post(select_url(acme), {"next": target})
    assert response.headers.get("Location") == expected


def test_the_page_carries_a_safe_next_into_its_forms(client: Client) -> None:
    """A safe next in the query string is echoed into the forms, an unsafe one is dropped.

    Args:
        client: Django test client fixture.
    """
    user = make_user()
    join(user, "Acme")
    client.force_login(user)
    assert 'name="next" value="/admin/"' in text(client.get(PAGE, {"next": "/admin/"}))
    assert 'name="next"' not in text(client.get(PAGE, {"next": "https://evil.example/"}))


def test_creating_a_company_makes_the_user_its_owner(client: Client) -> None:
    """A user with no membership creates a company and becomes its owner.

    Args:
        client: Django test client fixture.
    """
    user = make_user()
    client.force_login(user)
    response = client.post(PAGE, {"name": "Initech"})
    assert response.status_code == 302
    assert response.headers.get("Location") == PAGE
    membership = Membership.objects.get(user=user)
    assert (membership.tenant.name, membership.role) == ("Initech", "owner")


def test_creating_a_company_makes_it_the_active_tenant(client: Client) -> None:
    """The new company becomes the active tenant, even when another one was active.

    Args:
        client: Django test client fixture.
    """
    user = make_user()
    client.force_login(user)
    client.post(PAGE, {"name": "Acme"})
    client.post(PAGE, {"name": "Initech"})
    created = Membership.objects.get(user=user, tenant__name="Initech").tenant_id
    assert client.session["tenant_id"] == str(created)


def test_creating_a_company_follows_a_safe_next_url(client: Client) -> None:
    """The redirect after creation honors a local next URL.

    Args:
        client: Django test client fixture.
    """
    client.force_login(make_user())
    response = client.post(PAGE, {"name": "Initech", "next": "/admin/"})
    assert response.headers.get("Location") == "/admin/"


@pytest.mark.parametrize("name", ["", "   ", "x" * 201])
def test_an_invalid_company_name_is_rejected_with_an_error(client: Client, name: str) -> None:
    """A blank or overlong name re-renders the form with an error and creates nothing.

    Args:
        client: Django test client fixture.
        name: Submitted company name.
    """
    client.force_login(make_user())
    response = client.post(PAGE, {"name": name})
    assert response.status_code == 200
    assert "errors form-field" in text(response)
    assert Tenant.objects.count() == 0


def test_login_selects_the_only_membership(client: Client) -> None:
    """Signing in with one membership selects it and lands on the page.

    Args:
        client: Django test client fixture.
    """
    user = make_user()
    acme = join(user, "Acme")
    response = client.post("/accounts/login/", {"login": user.email, "password": PASSWORD})
    assert response.headers.get("Location") == PAGE
    assert client.session["tenant_id"] == str(acme.pk)


@pytest.mark.parametrize("count", [0, 2])
def test_login_selects_nothing_with_no_or_several_memberships(client: Client, count: int) -> None:
    """Signing in with zero or several memberships leaves the choice to the user.

    Args:
        client: Django test client fixture.
        count: Number of memberships of the user.
    """
    user = make_user()
    for index in range(count):
        join(user, f"T{index}")
    response = client.post("/accounts/login/", {"login": user.email, "password": PASSWORD})
    assert response.headers.get("Location") == PAGE
    assert "tenant_id" not in client.session


def test_settings_point_unfold_and_the_login_at_the_page() -> None:
    """The login redirect, the Return to site link and the dropdown all lead to the page."""
    assert reverse(settings.LOGIN_REDIRECT_URL) == PAGE
    assert settings.UNFOLD["SITE_URL"] == PAGE
    unfold: dict[str, Any] = settings.UNFOLD
    entry = unfold["SITE_DROPDOWN"][0]
    assert (entry["title"], entry["link"]) == ("Switch tenant", PAGE)


def test_the_admin_offers_a_switch_tenant_link(client: Client) -> None:
    """Staff reach the page from the site dropdown of the admin.

    Args:
        client: Django test client fixture.
    """
    client.force_login(make_user(staff=True))
    body = text(client.get("/admin/"))
    assert 'href="/tenants/"' in body
    assert "Switch tenant" in body
