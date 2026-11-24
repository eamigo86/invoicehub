"""TenantMiddleware applies the session tenant only to authenticated members of that tenant."""

import uuid
from typing import Any

import pytest
from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext

from accounts.models import User
from tenancy.context import get_current_tenant_id
from tenancy.middleware import TenantMiddleware
from tenancy.models import Membership, Tenant
from tenancy.permissions import Permission, Principal, request_principal

A = uuid.uuid4()


def run(
    session: dict[str, object], user: Any = None
) -> tuple[uuid.UUID | None, uuid.UUID | None, Principal | None]:
    """Pass a request carrying a session and a user through the middleware.

    Args:
        session: Session contents attached to the request.
        user: User of the request, anonymous when omitted.

    Returns:
        The tenant and principal seen inside the view and the tenant left after the request.
    """
    seen: list[tuple[uuid.UUID | None, Principal | None]] = []

    def view(request: HttpRequest) -> HttpResponse:
        """Record the current tenant and principal.

        Args:
            request: Incoming request.

        Returns:
            An empty response.
        """
        seen.append((get_current_tenant_id(), request_principal(request)))
        return HttpResponse()

    request = RequestFactory().get("/")
    request.session = session  # type: ignore[assignment]
    request.user = user or AnonymousUser()
    TenantMiddleware(view)(request)
    return seen[0][0], get_current_tenant_id(), seen[0][1]


def member(role: Membership.Role, tenant: Tenant | None = None) -> tuple[User, Tenant]:
    """Create a user holding a role in a tenant.

    Args:
        role: Role of the new membership.
        tenant: Tenant to join, a new one when omitted.

    Returns:
        The user and the tenant.
    """
    tenant = tenant or Tenant.objects.create(name="A")
    user = User.objects.create_user("ann", "ann@example.com", "pw")
    Membership.objects.create(user=user, tenant=tenant, role=role)
    return user, tenant


@pytest.mark.django_db
def test_member_gets_the_session_tenant_and_a_principal() -> None:
    """A member sees the tenant and a principal built from the role, reset afterwards."""
    user, tenant = member(Membership.Role.VIEWER)
    seen, after, principal = run({"tenant_id": str(tenant.pk)}, user)
    assert (seen, after) == (tenant.pk, None)
    assert principal == Principal(tenant.pk, frozenset({Permission.INVOICES_READ}))


@pytest.mark.django_db
def test_non_member_gets_no_tenant_and_the_key_is_dropped() -> None:
    """A user without a membership cannot enter a tenant by forging the session."""
    user, _ = member(Membership.Role.OWNER)
    other = Tenant.objects.create(name="B")
    session: dict[str, object] = {"tenant_id": str(other.pk)}
    assert run(session, user) == (None, None, None)
    assert "tenant_id" not in session


@pytest.mark.django_db
def test_anonymous_user_gets_no_tenant_and_the_key_is_dropped() -> None:
    """An anonymous request with a session tenant gets nothing."""
    tenant = Tenant.objects.create(name="A")
    session: dict[str, object] = {"tenant_id": str(tenant.pk)}
    assert run(session) == (None, None, None)
    assert "tenant_id" not in session


@pytest.mark.django_db
def test_superuser_without_membership_gets_no_tenant() -> None:
    """There is no superuser bypass: staff need a membership too."""
    tenant = Tenant.objects.create(name="A")
    root = User.objects.create_superuser("root", "root@example.com", "pw")
    assert run({"tenant_id": str(tenant.pk)}, root) == (None, None, None)


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["nope", 5, None])
def test_invalid_session_tenant_means_no_tenant(value: object) -> None:
    """A malformed session value leaves a member without a tenant and drops the key.

    Args:
        value: Session value under test.
    """
    user, _ = member(Membership.Role.OWNER)
    session: dict[str, object] = {"tenant_id": value}
    assert run(session, user) == (None, None, None)
    assert "tenant_id" not in session


@pytest.mark.django_db
def test_missing_session_tenant_means_no_tenant() -> None:
    """A member with an empty session has no tenant."""
    user, _ = member(Membership.Role.OWNER)
    assert run({}, user) == (None, None, None)


@pytest.mark.django_db
def test_tenant_is_reset_when_the_view_raises() -> None:
    """An exception in the view cannot leak the tenant to the next request on the thread."""
    user, tenant = member(Membership.Role.OWNER)

    def view(request: HttpRequest) -> HttpResponse:
        """Fail like a broken view.

        Args:
            request: Incoming request, unused.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("boom")

    request = RequestFactory().get("/")
    request.session = {"tenant_id": str(tenant.pk)}  # type: ignore[assignment]
    request.user = user
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
    user, tenant = member(Membership.Role.VIEWER)
    client.force_login(user)
    assert probe(client, str(tenant.pk)) == str(tenant.pk)
    assert probe(client, None) == ""
    assert probe(client, "not-a-uuid") == ""


@pytest.mark.django_db
def test_a_session_without_tenant_skips_the_membership_lookup() -> None:
    """Requests that name no tenant cost no membership query."""
    user, _ = member(Membership.Role.VIEWER)
    with CaptureQueriesContext(connection) as queries:
        run({}, user)
    assert not [q for q in queries.captured_queries if "tenancy_membership" in q["sql"]]
