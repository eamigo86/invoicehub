"""Admin registration for the tenant registry."""

from typing import Any

from django.contrib import admin
from django.http import HttpRequest
from unfold.admin import ModelAdmin  # type: ignore[import-untyped]

from tenancy.context import get_current_tenant_id

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
