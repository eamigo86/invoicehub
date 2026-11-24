"""Template context processors shared by every app."""

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

ALLAUTH_PACKAGE = "allauth"
CACHE_ATTRIBUTE = "_allauth_unfold_context"


def allauth_unfold_context(request: HttpRequest) -> dict[str, Any]:
    """Provide the Unfold admin site context on django-allauth pages.

    The gate is the module of the resolved view: allauth views are class based views whose
    as_view function keeps the module of their class, so any view defined inside the allauth
    package qualifies, including future ones. Requests that did not resolve, such as a 404, and
    every other page pay nothing. Unfold components rendered from a page template run the
    processors again with the same request, so the result is cached on the request.

    Args:
        request: Current request.

    Returns:
        The admin site context for an allauth view, otherwise an empty dictionary.
    """
    match = request.resolver_match
    if match is None or match.func.__module__.split(".")[0] != ALLAUTH_PACKAGE:
        return {}
    if not hasattr(request, CACHE_ATTRIBUTE):
        setattr(request, CACHE_ATTRIBUTE, admin.site.each_context(request))
    context: dict[str, Any] = getattr(request, CACHE_ATTRIBUTE)
    return context
