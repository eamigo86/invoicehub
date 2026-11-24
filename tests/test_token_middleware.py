"""TenantMiddleware resolves the tenant from Bearer API tokens."""

import json
import uuid

import pytest
from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory

from accounts.models import User
from tenancy.context import get_current_tenant_id
from tenancy.middleware import TenantMiddleware
from tenancy.models import Membership, Tenant
from tenancy.permissions import Permission, Principal, request_principal
from tenancy.tokens import issue_token, revoke_token

pytestmark = pytest.mark.django_db


class Seen:
    """What a view observed, and whether it ran at all.

    Attributes:
        called: Whether the view ran.
        tenant_id: Tenant active inside the view.
        principal: Principal attached to the request inside the view.
        user: User of the request inside the view.
    """

    def __init__(self) -> None:
        """Start with nothing seen."""
        self.called = False
        self.tenant_id: uuid.UUID | None = None
        self.principal: Principal | None = None
        self.user: object = None


def run(
    authorization: str | None, session: dict[str, object] | None = None, user: User | None = None
) -> tuple[HttpResponse, Seen]:
    """Pass a request with an Authorization header through the middleware.

    Args:
        authorization: Value of the Authorization header, or None for no header.
        session: Session contents attached to the request.
        user: User of the request, anonymous when omitted.

    Returns:
        The response and what the view saw.
    """
    seen = Seen()

    def view(request: HttpRequest) -> HttpResponse:
        """Record what the middleware set up.

        Args:
            request: Incoming request.

        Returns:
            An empty response.
        """
        seen.called = True
        seen.tenant_id = get_current_tenant_id()
        seen.principal = request_principal(request)
        seen.user = request.user
        return HttpResponse()

    headers = {} if authorization is None else {"Authorization": authorization}
    request = RequestFactory().get("/", headers=headers)
    request.session = session if session is not None else {}  # type: ignore[assignment]
    request.user = user or AnonymousUser()
    return TenantMiddleware(view)(request), seen


def issued(scopes: list[str]) -> tuple[str, Tenant]:
    """Issue a token for a new tenant.

    Args:
        scopes: Scopes of the token.

    Returns:
        The plaintext token and its tenant.
    """
    tenant = Tenant.objects.create(name="A")
    owner = User.objects.create_user("owner", "owner@example.com", "pw")
    membership = Membership.objects.create(user=owner, tenant=tenant, role=Membership.Role.OWNER)
    _, plaintext = issue_token(Principal.from_membership(membership), "ci", scopes)
    return plaintext, tenant


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_a_valid_token_sets_its_tenant_and_scope_principal(scheme: str) -> None:
    """The scheme is case-insensitive and the view runs as the token, then resets.

    Args:
        scheme: Spelling of the Bearer scheme.
    """
    plaintext, tenant = issued(["invoices:read"])
    response, seen = run(f"{scheme} {plaintext}")
    assert response.status_code == 200
    assert seen.tenant_id == tenant.pk
    assert seen.principal == Principal(tenant.pk, frozenset({Permission.INVOICES_READ}))
    assert get_current_tenant_id() is None


def test_a_token_replaces_the_session_identity_and_leaves_the_session_alone() -> None:
    """A session user and tenant never mix with token authority."""
    plaintext, tenant = issued(["invoices:read"])
    other = Tenant.objects.create(name="B")
    ann = User.objects.create_user("ann", "ann@example.com", "pw")
    Membership.objects.create(user=ann, tenant=other, role=Membership.Role.OWNER)
    session: dict[str, object] = {"tenant_id": str(other.pk)}
    _, seen = run(f"Bearer {plaintext}", session, ann)
    assert seen.tenant_id == tenant.pk
    assert isinstance(seen.user, AnonymousUser)
    assert session == {"tenant_id": str(other.pk)}


@pytest.mark.parametrize(
    "authorization",
    ["Bearer", "Bearer ", "Bearer nope", "Bearer ih_" + "0" * 43, "Bearer ih_\ud800"],
)
def test_a_bad_token_is_answered_401_without_calling_the_view(authorization: str) -> None:
    """Malformed and unknown tokens get a JSON 401 with a Bearer challenge.

    Args:
        authorization: Authorization header value under test.
    """
    response, seen = run(authorization)
    assert response.status_code == 401
    assert response["WWW-Authenticate"] == 'Bearer error="invalid_token"'
    assert json.loads(response.content) == {"detail": "Invalid token."}
    assert not seen.called
    assert get_current_tenant_id() is None


def test_a_revoked_token_is_answered_401() -> None:
    """Revoking a token stops it at the next request."""
    plaintext, tenant = issued(["invoices:read"])
    admin = Principal(tenant.pk, frozenset({Permission.TOKENS_MANAGE}))
    revoke_token(admin, tenant.api_tokens.get().pk)
    response, seen = run(f"Bearer {plaintext}")
    assert (response.status_code, seen.called) == (401, False)


def test_other_authorization_schemes_follow_the_session_path() -> None:
    """A non-Bearer scheme is ignored, so a member session still resolves normally."""
    plaintext, tenant = issued(["invoices:read"])
    ann = User.objects.create_user("ann", "ann@example.com", "pw")
    Membership.objects.create(user=ann, tenant=tenant, role=Membership.Role.VIEWER)
    _, seen = run(f"Basic {plaintext}", {"tenant_id": str(tenant.pk)}, ann)
    assert seen.tenant_id == tenant.pk
    assert seen.user == ann
    assert seen.principal == Principal(tenant.pk, frozenset({Permission.INVOICES_READ}))
