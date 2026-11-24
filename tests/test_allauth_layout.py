"""Every django-allauth account screen renders through the Unfold layout."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest
from allauth.account import urls as account_urls
from allauth.account.models import EmailAddress
from django.contrib import admin
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.core.mail import EmailMessage
from django.test import Client, RequestFactory
from django.urls import resolve, reverse

from accounts.models import User
from core.context_processors import allauth_unfold_context

pytestmark = pytest.mark.django_db

EMAIL = "ann@example.com"
PASSWORD = "S3cure-passw0rd!"
MARKER = 'data-allauth-layout="unfold"'
MENU_MARKER = 'data-allauth-menu="unfold"'


@pytest.fixture(autouse=True)
def fresh_rate_limits() -> None:
    """Clear the cache so allauth rate limits do not leak between tests."""
    cache.clear()


def make_user(*, staff: bool = False) -> User:
    """Create a user with a verified primary email address.

    Args:
        staff: Whether the user is a staff member with all admin permissions.

    Returns:
        The stored user.
    """
    if staff:
        user = User.objects.create_superuser("ann", EMAIL, PASSWORD)
    else:
        user = User.objects.create_user("ann", EMAIL, PASSWORD)
    EmailAddress.objects.create(user=user, email=EMAIL, verified=True, primary=True)
    return user


def text(response: Any) -> str:
    """Decode a response body.

    Args:
        response: Response to decode.

    Returns:
        The body as text.
    """
    return str(response.content.decode())


def test_login_page_renders_in_the_unfold_layout(client: Client) -> None:
    """The login page carries the layout marker and the Unfold stylesheet.

    Args:
        client: Django test client fixture.
    """
    response = client.get("/accounts/login/")
    body = text(response)
    assert response.status_code == 200
    assert MARKER in body
    assert "unfold/css/styles.css" in body
    assert "<title>" in body
    assert "Sign In" in body.split("</title>")[0]


def test_extra_head_and_extra_body_blocks_still_render(client: Client) -> None:
    """Blocks filled by allauth templates keep working in the Unfold layout.

    Args:
        client: Django test client fixture.
    """
    make_user()
    client.force_login(User.objects.get(email=EMAIL))
    body = text(client.get("/accounts/email/"))
    assert "account/js/account.js" in body
    assert MARKER in body


def test_messages_render_on_the_next_page(client: Client) -> None:
    """Flash messages show up in the layout after a login.

    Args:
        client: Django test client fixture.
    """
    make_user()
    response = client.post("/accounts/login/", {"login": EMAIL, "password": PASSWORD}, follow=True)
    body = text(response)
    assert "Successfully signed in" in body
    assert 'data-allauth-messages="unfold"' in body


def test_manage_pages_list_the_account_links_for_a_signed_in_user(client: Client) -> None:
    """Account management pages show links to email, password change and sign out.

    Args:
        client: Django test client fixture.
    """
    client.force_login(make_user())
    body = text(client.get("/accounts/password/change/"))
    assert MENU_MARKER in body
    for path in ("/accounts/email/", "/accounts/password/change/", "/accounts/logout/"):
        assert f'href="{path}"' in body


def test_entrance_pages_have_no_account_menu(client: Client) -> None:
    """Anonymous entrance pages do not render the management menu.

    Args:
        client: Django test client fixture.
    """
    assert MENU_MARKER not in text(client.get("/accounts/login/"))


def test_allauth_page_receives_the_unfold_context(client: Client) -> None:
    """An allauth response is rendered with the admin site context.

    Args:
        client: Django test client fixture.
    """
    response = client.get("/accounts/login/")
    assert response.context["site_title"] == "InvoiceHub"
    assert "styles" in response.context


def test_site_context_is_built_once_per_request(client: Client) -> None:
    """Unfold components re-run the processors, so the context is cached on the request.

    Args:
        client: Django test client fixture.
    """
    client.force_login(make_user())
    with patch.object(admin.site, "each_context", wraps=admin.site.each_context) as spy:
        client.get("/accounts/email/")
    assert spy.call_count == 1


def test_processor_skips_pages_outside_allauth() -> None:
    """The processor adds nothing for admin pages or unresolved requests."""
    admin_request = RequestFactory().get("/admin/")
    admin_request.resolver_match = resolve("/admin/")
    assert allauth_unfold_context(admin_request) == {}
    assert allauth_unfold_context(RequestFactory().get("/nowhere/")) == {}


def test_processor_adds_the_site_context_on_allauth_pages() -> None:
    """The processor returns the admin each_context for an allauth view."""
    request = RequestFactory().get("/accounts/login/")
    request.user = AnonymousUser()
    request.resolver_match = resolve("/accounts/login/")
    assert allauth_unfold_context(request)["site_title"] == "InvoiceHub"


def test_anonymous_admin_login_redirects_to_allauth(client: Client) -> None:
    """The Django admin login page is replaced by the allauth login.

    Args:
        client: Django test client fixture.
    """
    response = client.get("/admin/login/?next=/admin/")
    assert response.status_code == 302
    assert response["Location"] == "/accounts/login/?next=%2Fadmin%2F"


def test_verified_staff_user_reaches_the_admin_through_allauth(client: Client) -> None:
    """A staff user who signs in through allauth lands in the admin.

    Args:
        client: Django test client fixture.
    """
    make_user(staff=True)
    response = client.post(
        "/accounts/login/?next=/admin/", {"login": EMAIL, "password": PASSWORD}, follow=True
    )
    assert response.status_code == 200
    assert response.redirect_chain[-1][0] == "/admin/"


def test_signed_in_user_without_staff_is_refused_by_the_admin_login(client: Client) -> None:
    """A non-staff user visiting the admin login gets a 403.

    Args:
        client: Django test client fixture.
    """
    client.force_login(make_user())
    assert client.get("/admin/login/").status_code == 403


@dataclass(frozen=True)
class Case:
    """How to reach one allauth account URL and what it does in this configuration.

    Attributes:
        signed_in: Whether a verified user is signed in before the request.
        outcome: Either screen, redirect or status.
        expected: Redirect target for redirect, HTTP status for status, unused for screen.
        reason: Why the URL does not render a screen, empty for screens.
        kwargs: Builds the URL kwargs from the client and the mails sent so far.
    """

    signed_in: bool
    outcome: str
    expected: str | int = ""
    reason: str = ""
    kwargs: Callable[[Client, list[EmailMessage]], dict[str, str]] | None = None


def no_kwargs(client: Client, outbox: list[EmailMessage]) -> dict[str, str]:
    """Return no URL arguments.

    Args:
        client: Client used for the scenario.
        outbox: Captured outgoing messages.

    Returns:
        An empty dictionary.
    """
    return {}


def confirm_key(client: Client, outbox: list[EmailMessage]) -> dict[str, str]:
    """Sign up to receive a real confirmation mail and return its key.

    Args:
        client: Client used for the scenario.
        outbox: Captured outgoing messages.

    Returns:
        The key kwarg of the confirmation link.
    """
    client.post(
        "/accounts/signup/",
        {"email": "new@example.com", "password1": PASSWORD, "password2": PASSWORD},
    )
    client.logout()
    match = re.search(r"/accounts/confirm-email/([^/\s]+)/", str(outbox[-1].body))
    assert match
    return {"key": match.group(1)}


def reset_key(client: Client, outbox: list[EmailMessage]) -> dict[str, str]:
    """Request a password reset to receive a real mail and return its link kwargs.

    Args:
        client: Client used for the scenario.
        outbox: Captured outgoing messages.

    Returns:
        The uidb36 and key kwargs of the reset link.
    """
    client.post("/accounts/password/reset/", {"email": EMAIL})
    match = re.search(r"/accounts/password/reset/key/([0-9a-z]+)-([^/\s]+)/", str(outbox[-1].body))
    assert match
    return {"uidb36": match.group(1), "key": match.group(2)}


LOGIN = "/accounts/login/"
CASES: dict[str, Case] = {
    "account_login": Case(False, "screen"),
    "account_logout": Case(True, "screen"),
    "account_inactive": Case(False, "screen"),
    "account_signup": Case(False, "screen"),
    "account_reauthenticate": Case(True, "screen"),
    "account_request_reauthentication_code": Case(True, "screen"),
    "account_confirm_reauthentication_code": Case(
        True,
        "redirect",
        "/accounts/reauthenticate/code/",
        "no reauthentication code is pending, the user is sent to request one",
    ),
    "account_email": Case(True, "screen"),
    "account_email_verification_sent": Case(False, "screen"),
    "account_change_password": Case(True, "screen"),
    "account_set_password": Case(
        True, "redirect", "/accounts/password/change/", "the user already has a password"
    ),
    "account_reset_password": Case(False, "screen"),
    "account_confirm_login_code": Case(
        False, "redirect", LOGIN, "no login by code is pending in this configuration"
    ),
    "account_confirm_email": Case(False, "screen", kwargs=confirm_key),
    "account_reset_password_from_key_done": Case(False, "screen"),
    "account_reset_password_from_key": Case(False, "screen", kwargs=reset_key),
    "account_reset_password_done": Case(False, "screen"),
}


def account_url_names() -> list[str]:
    """Collect the URL names of allauth's account URLconf.

    Returns:
        The names of every named pattern, in declaration order.
    """
    return [str(p.name) for p in account_urls.urlpatterns if getattr(p, "name", None)]


def test_every_account_url_name_has_a_case() -> None:
    """A new allauth URL name fails the build until the table covers it."""
    assert sorted(account_url_names()) == sorted(CASES)


@pytest.mark.parametrize("name", account_url_names())
def test_account_url_renders_through_the_unfold_layout(
    client: Client, mailoutbox: list[EmailMessage], name: str
) -> None:
    """Each account URL renders a layout screen, redirects or is disabled, as tabled.

    Args:
        client: Django test client fixture.
        mailoutbox: Captured outgoing messages.
        name: URL name under test.
    """
    case = CASES[name]
    make_user()
    kwargs = (case.kwargs or no_kwargs)(client, mailoutbox)
    if case.signed_in:
        client.force_login(User.objects.get(email=EMAIL))
    url: Any = reverse(name, kwargs=kwargs)
    response = client.get(url, follow=case.outcome == "screen")
    if case.outcome == "screen":
        assert response.status_code == 200, case.reason
        assert MARKER in text(response)
    elif case.outcome == "redirect":
        assert response.status_code == 302, case.reason
        assert response["Location"] == case.expected, case.reason
    else:
        assert response.status_code == case.expected, case.reason
