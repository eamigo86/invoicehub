"""The GraphQL endpoint, open to API tokens only."""

from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django_graphex.security import DisableIntrospectionMiddleware
from django_graphex.views import GraphQLView

from api.ratelimit import retry_after
from api.schema import schema
from tenancy.permissions import request_principal


class ProtectedGraphQLView(GraphQLView):
    """GraphQL view that refuses schema introspection unless DEBUG or the token allows it."""

    def get_middleware(self, request: HttpRequest) -> Any:
        """Choose the execution middleware for one request.

        Introspection is open when DEBUG is on or when the request token is a development
        token, and refused for every other request. The configured graphex middleware is kept
        in both cases.

        Args:
            request: Incoming request, already authenticated by the token middleware.

        Returns:
            The configured chain, followed by the introspection blocker unless it is allowed.
        """
        chain = list(super().get_middleware(request) or [])
        principal = request_principal(request)
        if settings.DEBUG or (principal is not None and principal.allow_introspection):
            return chain
        return [*chain, DisableIntrospectionMiddleware()]


_graphql = ProtectedGraphQLView.as_view(schema=schema)


@csrf_exempt
def graphql(request: HttpRequest) -> HttpResponse:
    """Run a GraphQL request for an API token principal.

    CSRF protection is off because the credential is a bearer token that a browser never
    attaches by itself. The tenant is the one of the token, which the middleware has already
    made active.

    Args:
        request: Incoming request.

    Returns:
        The GraphQL response, a 401 JSON response when the request carries no API token, or a
        429 JSON response with Retry-After when the token is over its rate limit.
    """
    principal = request_principal(request)
    if principal is None or principal.token_id is None:
        response = JsonResponse({"detail": "An API token is required."}, status=401)
        response["WWW-Authenticate"] = "Bearer"
        return response
    wait = retry_after(principal.token_id)
    if wait is not None:
        response = JsonResponse({"detail": "Rate limit exceeded."}, status=429)
        response["Retry-After"] = str(wait)
        return response
    result: HttpResponse = _graphql(request)
    return result
