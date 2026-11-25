"""The GraphQL endpoint, open to API tokens only."""

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django_graphex.views import GraphQLView

from api.schema import schema
from tenancy.permissions import request_principal

_graphql = GraphQLView.as_view(schema=schema)


@csrf_exempt
def graphql(request: HttpRequest) -> HttpResponse:
    """Run a GraphQL request for an API token principal.

    CSRF protection is off because the credential is a bearer token that a browser never
    attaches by itself. The tenant is the one of the token, which the middleware has already
    made active.

    Args:
        request: Incoming request.

    Returns:
        The GraphQL response, or a 401 JSON response when the request carries no API token.
    """
    principal = request_principal(request)
    if principal is None or principal.token_id is None:
        response = JsonResponse({"detail": "An API token is required."}, status=401)
        response["WWW-Authenticate"] = "Bearer"
        return response
    result: HttpResponse = _graphql(request)
    return result
