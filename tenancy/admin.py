"""Admin registration for the tenant registry."""

from typing import Any

from django.contrib import admin
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from unfold.admin import ModelAdmin  # type: ignore[import-untyped]
from unfold.decorators import action  # type: ignore[import-untyped]

from tenancy.context import get_current_tenant_id, parse_tenant_id

from .models import Tenant


class TenantModelAdmin(ModelAdmin):  # type: ignore[misc]
    """Base admin for tenant models, hidden entirely while no tenant is active.

    Without an active tenant the default manager would raise TenantNotSet, so every permission
    is denied instead and the model disappears from the admin.
    """

    def has_module_permission(self, request: HttpRequest) -> bool:
        """Show the model in the admin index only while a tenant is active.

        Args:
            request: Current request.

        Returns:
            True when a tenant is active and the user may see the module.
        """
        return get_current_tenant_id() is not None and bool(super().has_module_permission(request))

    def has_view_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Allow viewing only while a tenant is active.

        Args:
            request: Current request.
            obj: Object being viewed, if any.

        Returns:
            True when a tenant is active and the user may view.
        """
        return get_current_tenant_id() is not None and bool(
            super().has_view_permission(request, obj)
        )

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Allow adding only while a tenant is active.

        Args:
            request: Current request.

        Returns:
            True when a tenant is active and the user may add.
        """
        return get_current_tenant_id() is not None and bool(super().has_add_permission(request))

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Allow changing only while a tenant is active.

        Args:
            request: Current request.
            obj: Object being changed, if any.

        Returns:
            True when a tenant is active and the user may change.
        """
        return get_current_tenant_id() is not None and bool(
            super().has_change_permission(request, obj)
        )

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Allow deleting only while a tenant is active.

        Args:
            request: Current request.
            obj: Object being deleted, if any.

        Returns:
            True when a tenant is active and the user may delete.
        """
        return get_current_tenant_id() is not None and bool(
            super().has_delete_permission(request, obj)
        )


@admin.register(Tenant)
class TenantAdmin(ModelAdmin):  # type: ignore[misc]
    """Unfold-styled admin listing tenants by name and creation time."""

    list_display = ("name", "created_at")
    search_fields = ("name",)
    actions_row = ["activate"]

    def has_activate_permission(self, request: HttpRequest) -> bool:
        """Restrict the Activate action to superusers.

        Args:
            request: Current request.

        Returns:
            True when the user is a superuser.
        """
        return bool(request.user.is_superuser)

    @action(description="Activate", permissions=["activate"])  # type: ignore[untyped-decorator]
    def activate(self, request: HttpRequest, object_id: str) -> HttpResponse:
        """Ask for confirmation, then make a tenant the active one for the admin session.

        GET only renders a confirmation page. POST, protected by CSRF and limited to
        superusers, stores the tenant in the session.

        Args:
            request: Current request.
            object_id: Primary key of the tenant to activate.

        Returns:
            The confirmation page, or a redirect to the tenant changelist after activation.

        Raises:
            Http404: When the id is not a valid UUID or names no tenant.
        """
        try:
            tenant = get_object_or_404(Tenant, pk=parse_tenant_id(object_id))
        except ValueError as exc:
            raise Http404("invalid tenant id") from exc
        if request.method == "POST":
            # Provisional until the RFC 0002 authentication slice provides real tenant selection
            # with membership checks.
            request.session["tenant_id"] = str(tenant.pk)
            return redirect("admin:tenancy_tenant_changelist")
        return TemplateResponse(
            request,
            "tenancy/activate_tenant.html",
            {**self.admin_site.each_context(request), "title": "Activate tenant", "tenant": tenant},
        )
