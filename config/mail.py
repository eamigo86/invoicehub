"""Translate an EMAIL_URL into the Django MAILERS setting."""

from typing import Any
from urllib.parse import parse_qs, urlsplit

import environ  # type: ignore[import-untyped]
from django.core.exceptions import ImproperlyConfigured

_SMTP = {
    "host": "EMAIL_HOST",
    "port": "EMAIL_PORT",
    "username": "EMAIL_HOST_USER",
    "password": "EMAIL_HOST_PASSWORD",
}
_TRUE = {"1", "true", "yes", "on"}


def _flag(query: dict[str, str], *names: str) -> bool:
    """Read a boolean query parameter.

    Args:
        query: Lower-cased query parameters.
        *names: Accepted parameter names.

    Returns:
        True when any of the names is set to a truthy value.
    """
    return any(query.get(n, "").lower() in _TRUE for n in names)


def build_mailers(url: str) -> dict[str, Any]:
    """Build the MAILERS setting from an email URL.

    The smtps and smtp+ssl schemes enable implicit SSL, smtp+tls enables STARTTLS, and the
    query parameters tls, ssl, timeout, ssl_keyfile and ssl_certfile are honored.

    Args:
        url: Email URL such as smtp://user:pass@host:25 or consolemail://.

    Returns:
        A MAILERS dictionary with a single default mailer.

    Raises:
        ImproperlyConfigured: If TLS and SSL are both requested.
    """
    email = environ.Env.email_url_config(url)
    backend = email["EMAIL_BACKEND"]
    if not email["EMAIL_HOST"]:
        return {"default": {"BACKEND": backend, "OPTIONS": {}}}
    parts = urlsplit(url)
    query = {k.lower(): v[0] for k, v in parse_qs(parts.query).items()}
    use_ssl = parts.scheme in {"smtps", "smtp+ssl"} or _flag(query, "ssl", "use_ssl")
    use_tls = parts.scheme == "smtp+tls" or _flag(query, "tls", "use_tls")
    if use_ssl and use_tls:
        raise ImproperlyConfigured("EMAIL_URL cannot enable both TLS and SSL")
    options: dict[str, Any] = {k: email[v] for k, v in _SMTP.items() if email[v]}
    if use_ssl:
        options["use_ssl"] = True
    if use_tls:
        options["use_tls"] = True
    if "timeout" in query:
        options["timeout"] = int(query["timeout"])
    for key in ("ssl_keyfile", "ssl_certfile"):
        if key in query:
            options[key] = query[key]
    return {"default": {"BACKEND": backend, "OPTIONS": options}}
