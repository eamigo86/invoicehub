"""Request middleware that resolves the active tenant."""

from collections.abc import Callable

from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest, HttpResponse, JsonResponse

from tenancy.context import current_tenant_id, parse_tenant_id
from tenancy.models import Membership
from tenancy.permissions import Principal, set_request_principal
from tenancy.tokens import authenticate_token


class TenantMiddleware:
    """Expose the session tenant to the rest of the request through the context variable.

    The session tenant is applied only when the authenticated user holds a membership in it;
    otherwise no tenant is applied and the stale session key is removed. Superusers get no
    bypass. A Bearer API token takes over instead: its tenant and scopes apply, the session is
    ignored and left untouched, and the request user becomes anonymous so a session identity
    never mixes with token authority. A bad Bearer token is answered 401 without reaching the
    view; other Authorization schemes are ignored. The acting principal is attached to the
    request for the permission service. It sits right after AuthenticationMiddleware so that
    the session and the user are available. It only sets the context variable: ATOMIC_REQUESTS
    wraps the view alone and this middleware runs outside that transaction, so a SET LOCAL here
    would have no effect. The database backend
    applies the variable when the view's transaction opens.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        """Store the next handler in the chain.

        Args:
            get_response: Callable that produces the response for a request.
        """
        self.get_response = get_response

    @staticmethod
    def _session_membership(request: HttpRequest) -> Membership | None:
        """Find the membership that entitles the user to the session tenant.

        Membership has no Row-Level Security, so the lookup works before a tenant is active.
        The session key is removed whenever it does not lead to a membership.

        Args:
            request: Incoming request, whose session may name a tenant.

        Returns:
            The membership of the user in the session tenant, or None.
        """
        membership = None
        try:
            tenant_id = parse_tenant_id(request.session.get("tenant_id"))
        except ValueError:
            pass
        else:
            if request.user.is_authenticated:
                membership = Membership.objects.filter(
                    user=request.user, tenant_id=tenant_id
                ).first()
        if membership is None:
            request.session.pop("tenant_id", None)
        return membership

    @staticmethod
    def _bearer(request: HttpRequest) -> str | None:
        """Read the credentials of a Bearer Authorization header.

        Args:
            request: Incoming request.

        Returns:
            The text after the Bearer scheme, empty when there is none, or None when the
            request carries no Bearer header, whatever the case of the scheme.
        """
        scheme, _, credentials = request.headers.get("Authorization", "").partition(" ")
        return credentials.strip() if scheme.lower() == "bearer" else None

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Run the request with the token or session tenant active, then reset it.

        Args:
            request: Incoming request, whose session may name a tenant.

        Returns:
            The response produced by the rest of the chain, or a 401 for a bad Bearer token.
        """
        principal: Principal | None
        raw = self._bearer(request)
        if raw is None:
            membership = self._session_membership(request)
            principal = Principal.from_membership(membership) if membership else None
            tenant_id = membership.tenant_id if membership else None
        else:
            api_token = authenticate_token(raw)
            if api_token is None:
                response = JsonResponse({"detail": "Invalid token."}, status=401)
                response["WWW-Authenticate"] = 'Bearer error="invalid_token"'
                return response
            request.user = AnonymousUser()
            principal = Principal.from_token(api_token)
            tenant_id = api_token.tenant_id
        set_request_principal(request, principal)
        token = current_tenant_id.set(tenant_id)
        try:
            return self.get_response(request)
        finally:
            current_tenant_id.reset(token)
