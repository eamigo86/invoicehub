"""Request middleware that resolves the active tenant."""

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from tenancy.context import current_tenant_id, parse_tenant_id


class TenantMiddleware:
    """Expose the session tenant to the rest of the request through the context variable.

    It sits right after AuthenticationMiddleware so that the upcoming membership check has the
    session and the user available. It only sets the context variable: ATOMIC_REQUESTS wraps
    the view alone and this middleware runs outside that transaction, so a SET LOCAL here would
    have no effect. The database backend applies the variable when the view's transaction opens.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        """Store the next handler in the chain.

        Args:
            get_response: Callable that produces the response for a request.
        """
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Run the request with the session tenant active, then reset it.

        Args:
            request: Incoming request, whose session may name a tenant.

        Returns:
            The response produced by the rest of the chain.
        """
        # Provisional until RFC 0002 authentication lands: the tenant is trusted as stored in
        # the session, without checking that the user is a member of it.
        try:
            tenant_id = parse_tenant_id(request.session.get("tenant_id"))
        except ValueError:
            tenant_id = None
        token = current_tenant_id.set(tenant_id)
        try:
            return self.get_response(request)
        finally:
            current_tenant_id.reset(token)
