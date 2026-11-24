"""TenantMiddleware resolves the tenant from the session for the duration of a request."""

import uuid

import pytest
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory

from tenancy.context import get_current_tenant_id
from tenancy.middleware import TenantMiddleware

A = uuid.uuid4()


def run(session: dict[str, object]) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Pass a request carrying a session through the middleware.

    Args:
        session: Session contents attached to the request.

    Returns:
        The tenant seen inside the view and the tenant left after the request.
    """
    seen: list[uuid.UUID | None] = []

    def view(request: HttpRequest) -> HttpResponse:
        """Record the current tenant.

        Args:
            request: Incoming request, unused.

        Returns:
            An empty response.
        """
        seen.append(get_current_tenant_id())
        return HttpResponse()

    request = RequestFactory().get("/")
    request.session = session  # type: ignore[assignment]
    TenantMiddleware(view)(request)
    return seen[0], get_current_tenant_id()


def test_valid_session_tenant_is_active_during_the_request() -> None:
    """A UUID in the session is the current tenant inside the view and gone afterwards."""
    assert run({"tenant_id": str(A)}) == (A, None)


@pytest.mark.parametrize(
    "session", [{}, {"tenant_id": "nope"}, {"tenant_id": 5}, {"tenant_id": None}]
)
def test_missing_or_invalid_session_tenant_means_no_tenant(session: dict[str, object]) -> None:
    """A missing or malformed session value leaves the request without a tenant.

    Args:
        session: Session contents under test.
    """
    assert run(session) == (None, None)


def test_tenant_is_reset_when_the_view_raises() -> None:
    """An exception in the view cannot leak the tenant to the next request on the thread."""

    def view(request: HttpRequest) -> HttpResponse:
        """Fail like a broken view.

        Args:
            request: Incoming request, unused.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("boom")

    request = RequestFactory().get("/")
    request.session = {"tenant_id": str(A)}  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        TenantMiddleware(view)(request)
    assert get_current_tenant_id() is None


def probe(client: Client, tenant: object | None) -> str:
    """Request the probe view with a given session tenant.

    Args:
        client: Test client whose session is prepared.
        tenant: Value stored as the session tenant, or None to store nothing.

    Returns:
        The tenant setting the view observed in its transaction.
    """
    session = client.session
    if tenant is None:
        session.pop("tenant_id", None)
    else:
        session["tenant_id"] = tenant
    session.save()
    return client.get("/probe/").content.decode()


@pytest.mark.django_db(transaction=True)
@pytest.mark.urls("tests.urls_tenant_probe")
def test_request_transaction_carries_the_session_tenant(client: Client) -> None:
    """End to end: the view's transaction sees the tenant, and it does not leak afterwards.

    Args:
        client: Django test client fixture.
    """
    assert probe(client, str(A)) == str(A)
    assert probe(client, None) == ""
    assert probe(client, "not-a-uuid") == ""
