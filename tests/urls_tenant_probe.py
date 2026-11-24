"""Test-only URLconf exposing the tenant setting seen by a view's transaction."""

from django.db import connection
from django.http import HttpRequest, HttpResponse
from django.urls import path


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


urlpatterns = [path("probe/", probe)]
