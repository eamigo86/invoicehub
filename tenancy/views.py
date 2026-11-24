"""Views of the tenant selection page. They only call the tenancy services."""

import uuid
from typing import cast

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts.models import User
from tenancy.context import get_current_tenant_id
from tenancy.forms import CompanyForm
from tenancy.services import create_tenant, list_memberships, select_tenant


def safe_next(request: HttpRequest) -> str:
    """Read the next URL of a request, keeping it only when it stays on this site.

    Args:
        request: Current request, whose POST or GET data may carry a next field.

    Returns:
        The next URL when it is local, otherwise an empty string.
    """
    target = request.POST.get("next") or request.GET.get("next") or ""
    allowed = url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    )
    return target if allowed else ""


def signed_in(request: HttpRequest) -> User:
    """Return the user of a request that login_required has already vetted.

    Args:
        request: Current request of a signed in user.

    Returns:
        The signed in user.
    """
    return cast(User, request.user)


def redirect_after(request: HttpRequest) -> HttpResponseRedirect:
    """Redirect to the safe next URL, or back to the page.

    Args:
        request: Current request.

    Returns:
        The redirect response.
    """
    return redirect(safe_next(request) or "tenancy:list")


@login_required
def tenant_list(request: HttpRequest) -> HttpResponse:
    """List the user's companies, and create and select a new one on POST.

    Args:
        request: Current request of a signed in user.

    Returns:
        The page, or a redirect after a company was created.
    """
    form = CompanyForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        tenant = create_tenant(signed_in(request), form.cleaned_data["name"])
        select_tenant(request.session, signed_in(request), tenant.pk)
        return redirect_after(request)
    context = {
        "memberships": list_memberships(signed_in(request)),
        "active_tenant_id": get_current_tenant_id(),
        "form": form,
        "next": safe_next(request),
    }
    return render(request, "tenancy/tenants.html", context)


@login_required
@require_POST
def tenant_select(request: HttpRequest, tenant_id: uuid.UUID) -> HttpResponse:
    """Switch the session to one of the user's tenants.

    Args:
        request: Current POST request of a signed in user.
        tenant_id: Tenant to switch to.

    Returns:
        A redirect to the safe next URL or back to the page.
    """
    select_tenant(request.session, signed_in(request), tenant_id)
    return redirect_after(request)
