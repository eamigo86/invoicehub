"""EMAIL_URL to MAILERS mapping, including transport security."""

import pytest
from django.core.exceptions import ImproperlyConfigured

from config.mail import build_mailers


def options(url: str) -> dict[str, object]:
    """Return the default mailer OPTIONS built from a URL.

    Args:
        url: Email URL to translate.

    Returns:
        The OPTIONS dictionary of the default mailer.
    """
    mailer = build_mailers(url)["default"]
    assert isinstance(mailer["OPTIONS"], dict)
    return dict(mailer["OPTIONS"])


def test_console_default() -> None:
    """A console URL maps to the console backend without options."""
    mailer = build_mailers("consolemail://")["default"]
    assert mailer["BACKEND"].endswith("console.EmailBackend")
    assert mailer["OPTIONS"] == {}


def test_plain_smtp_has_no_encryption_and_carries_credentials() -> None:
    """Plain smtp keeps credentials and enables neither TLS nor SSL."""
    opts = options("smtp://user:secret@mail.example:25")
    assert opts == {"host": "mail.example", "port": 25, "username": "user", "password": "secret"}


def test_smtps_enables_ssl() -> None:
    """The smtps scheme turns implicit SSL on and keeps credentials."""
    opts = options("smtps://user:secret@mail.example:465")
    assert opts["use_ssl"] is True
    assert "use_tls" not in opts
    assert (opts["username"], opts["password"]) == ("user", "secret")


def test_smtp_plus_tls_enables_starttls() -> None:
    """The smtp+tls scheme turns STARTTLS on."""
    opts = options("smtp+tls://user:secret@mail.example:587")
    assert opts["use_tls"] is True
    assert "use_ssl" not in opts


def test_query_flags_and_timeout() -> None:
    """Query parameters can enable TLS and set a timeout and client certificate."""
    opts = options("smtp://mail.example:587?tls=True&timeout=7&ssl_certfile=/c.pem")
    assert opts["use_tls"] is True
    assert opts["timeout"] == 7
    assert opts["ssl_certfile"] == "/c.pem"


def test_tls_and_ssl_together_are_rejected() -> None:
    """Enabling both TLS and SSL is a configuration error."""
    with pytest.raises(ImproperlyConfigured):
        build_mailers("smtps://mail.example:465?tls=True")
