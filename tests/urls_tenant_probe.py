"""Test-only URLconf exposing the tenant setting and the customers seen by a view."""

from django.db import connection
from django.http import HttpRequest, HttpResponse
from django.urls import path

from billing.models import Customer


def probe(request: HttpRequest) -> HttpResponse:
    """Return the database tenant setting as seen inside the view.

    Args:
        request: Incoming request, unused.

    Returns:
        A plain text response holding app.tenant_id, empty when unset.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.tenant_id', true)")
        row = cursor.fetchone()
    assert row is not None
    return HttpResponse(row[0] or "")


def customers(request: HttpRequest) -> HttpResponse:
    """List the customers visible to the active tenant, plus the request user state.

    Args:
        request: Incoming request, whose user is reported.

    Returns:
        A plain text response whose first line says whether the user is authenticated and
        whose other lines are customer names in name order.
    """
    names = Customer.objects.order_by("name").values_list("name", flat=True)
    return HttpResponse("\n".join([f"authenticated={request.user.is_authenticated}", *names]))


urlpatterns = [path("probe/", probe), path("customers/", customers)]
