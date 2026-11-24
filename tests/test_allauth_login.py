"""Email and password login through django-allauth with mandatory email verification."""

import re
from collections.abc import Sequence
from typing import Any

import pytest
from django.apps import apps
from django.core.cache import cache
from django.core.mail import EmailMessage
from django.core.mail.backends.base import BaseEmailBackend
from django.test import Client

from accounts.models import User

pytestmark = pytest.mark.django_db

EMAIL = "ann@example.com"
PASSWORD = "S3cure-passw0rd!"
INTERIM_TARGET = "/accounts/email/"


class RecordingBackend(BaseEmailBackend):
    """Mail backend that remembers every message it was asked to send."""

    sent: list[EmailMessage] = []

    def send_messages(self, email_messages: Sequence[EmailMessage]) -> int:
        """Record the messages instead of delivering them.

        Args:
            email_messages: Messages to send.

        Returns:
            The number of messages recorded.
        """
        self.sent.extend(email_messages)
        return len(email_messages)


@pytest.fixture(autouse=True)
def fresh_rate_limits() -> None:
    """Clear the cache so allauth rate limits do not leak between tests."""
    cache.clear()


def signup(client: Client, email: str = EMAIL) -> Any:
    """Submit the signup form.

    Args:
        client: Test client.
        email: Email address to register.

    Returns:
        The signup response.
    """
    return client.post(
        "/accounts/signup/", {"email": email, "password1": PASSWORD, "password2": PASSWORD}
    )


def login(client: Client, identifier: str = EMAIL) -> Any:
    """Submit the login form.

    Args:
        client: Test client.
        identifier: Value typed in the login field.

    Returns:
        The login response.
    """
    return client.post("/accounts/login/", {"login": identifier, "password": PASSWORD})


def addresses() -> list[tuple[str, bool]]:
    """List the stored email addresses with their verification flag.

    Returns:
        Pairs of address and verified flag.
    """
    model = apps.get_model("account", "EmailAddress")
    return list(model.objects.values_list("email", "verified"))


def confirmation_path(outbox: list[EmailMessage]) -> str:
    """Extract the confirmation link path from the single verification email sent.

    Args:
        outbox: Captured outgoing messages.

    Returns:
        The path of the confirmation link.
    """
    assert len(outbox) == 1
    body = str(outbox[0].body)
    match = re.search(r"http://testserver(/accounts/confirm-email/[^/\s]+/)", body)
    assert match, body
    return match.group(1)


def test_signup_sends_a_verification_email_and_creates_an_unverified_address(
    client: Client, mailoutbox: list[EmailMessage]
) -> None:
    """Signing up mails a confirmation link and leaves the address unverified.

    Args:
        client: Django test client fixture.
        mailoutbox: Captured outgoing messages.
    """
    response = signup(client)
    assert response.status_code == 302
    assert response.url == "/accounts/confirm-email/"
    assert [m.to for m in mailoutbox] == [[EMAIL]]
    confirmation_path(mailoutbox)
    assert addresses() == [(EMAIL, False)]


def test_login_is_refused_until_the_address_is_verified(client: Client) -> None:
    """An unverified user is sent to the verification page and stays anonymous.

    Args:
        client: Django test client fixture.
    """
    signup(client)
    client.logout()
    response = login(client)
    assert response.status_code == 302
    assert response.url == "/accounts/confirm-email/"
    assert "_auth_user_id" not in client.session


def test_confirmation_link_verifies_the_address_and_login_then_succeeds(
    client: Client, mailoutbox: list[EmailMessage]
) -> None:
    """The emailed link verifies the address and login lands on the interim page.

    Args:
        client: Django test client fixture.
        mailoutbox: Captured outgoing messages.
    """
    signup(client)
    client.logout()
    assert client.post(confirmation_path(mailoutbox)).status_code == 302
    assert addresses() == [(EMAIL, True)]
    response = login(client)
    assert response.status_code == 302
    assert response.url == INTERIM_TARGET
    assert client.session["_auth_user_id"] == str(User.objects.get(email=EMAIL).pk)


@pytest.mark.parametrize("duplicate", ["ann@example.com", "ANN@Example.COM"])
def test_second_signup_with_the_same_email_is_rejected(
    client: Client, mailoutbox: list[EmailMessage], duplicate: str
) -> None:
    """A repeated email, in any case, creates no second user.

    Args:
        client: Django test client fixture.
        mailoutbox: Captured outgoing messages.
        duplicate: Email typed on the second signup.
    """
    signup(client)
    client.logout()
    signup(client, duplicate)
    assert User.objects.count() == 1
    assert len(addresses()) == 1


def test_login_with_the_username_does_not_authenticate(
    client: Client, mailoutbox: list[EmailMessage]
) -> None:
    """The username is not a login identifier even for a verified user.

    Args:
        client: Django test client fixture.
        mailoutbox: Captured outgoing messages.
    """
    signup(client)
    client.logout()
    client.post(confirmation_path(mailoutbox))
    username = User.objects.get(email=EMAIL).username
    response = login(client, username)
    assert response.status_code == 200
    assert "_auth_user_id" not in client.session


def test_django_admin_login_still_uses_the_username(client: Client) -> None:
    """The Django model backend stays active next to allauth.

    Args:
        client: Django test client fixture.
    """
    User.objects.create_superuser("root", "root@example.com", PASSWORD)
    assert client.login(username="root", password=PASSWORD)


def test_verification_mail_goes_through_the_mailers_setting(settings: Any, client: Client) -> None:
    """Allauth mail is sent by the mailer configured in MAILERS.

    Args:
        settings: pytest-django settings fixture.
        client: Django test client fixture.
    """
    settings.MAILERS = {"default": {"BACKEND": f"{__name__}.RecordingBackend", "OPTIONS": {}}}
    RecordingBackend.sent.clear()
    signup(client)
    assert [m.to for m in RecordingBackend.sent] == [[EMAIL]]
