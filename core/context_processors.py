"""Template context processors shared by every app."""

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

UNFOLD_CONTEXT_MODULES = ("allauth", "tenancy.views")
"""Modules whose views render in the Unfold layout and so need the admin site context."""

CACHE_ATTRIBUTE = "_allauth_unfold_context"


def allauth_unfold_context(request: HttpRequest) -> dict[str, Any]:
    """Provide the Unfold admin site context on the pages rendered in the Unfold layout.

    The gate is the module of the resolved view, matched against UNFOLD_CONTEXT_MODULES.
    Allauth views are class based views whose as_view function keeps the module of their class,
    so any view defined inside the allauth package qualifies, including future ones; the
    tenant selection page is admitted by its view module. Requests that did not resolve, such
    as a 404, and every other page pay nothing. Unfold components rendered from a page template
    run the processors again with the same request, so the result is cached on the request.

    Args:
        request: Current request.

    Returns:
        The admin site context for an admitted view, otherwise an empty dictionary.
    """
    match = request.resolver_match
    if match is None or not any(
        match.func.__module__ == m or match.func.__module__.startswith(f"{m}.")
        for m in UNFOLD_CONTEXT_MODULES
    ):
        return {}
    if not hasattr(request, CACHE_ATTRIBUTE):
        setattr(request, CACHE_ATTRIBUTE, admin.site.each_context(request))
    context: dict[str, Any] = getattr(request, CACHE_ATTRIBUTE)
    return context
