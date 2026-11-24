"""Admin registration for the tenant registry."""

from typing import Any

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db.models import QuerySet
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from unfold.admin import ModelAdmin, TabularInline  # type: ignore[import-untyped]
from unfold.decorators import action  # type: ignore[import-untyped]

from tenancy.context import get_current_tenant_id, parse_tenant_id
from tenancy.permissions import Permission, has_permission, request_principal

from .models import Membership, Tenant


class TenantModelAdmin(ModelAdmin):  # type: ignore[misc]
    """Base admin for tenant models, refused entirely while no tenant is active.

    Without an active tenant the default manager would raise TenantNotSet, so the model is
    left out of the index and every URL answers 403 instead. On top of Django's model
    permissions the acting principal needs read_permission to view and write_permission to add,
    change or delete; subclasses override them, for example with reconciliation:write.

    Attributes:
        read_permission: Permission required to view rows.
        write_permission: Permission required to add, change or delete rows.
    """

    read_permission = Permission.INVOICES_READ
    write_permission = Permission.INVOICES_WRITE

    def get_queryset(self, request: HttpRequest) -> QuerySet[Any]:
        """Return the model's rows, refusing the request while no tenant is active.

        Every admin view that reads or locates objects goes through this method, often before
        any permission check, so refusing here turns what would be a TenantNotSet server error
        into a 403 on the changelist, change, delete and history views and on bulk actions.

        Args:
            request: Current request.

        Returns:
            The queryset of the active tenant's rows.

        Raises:
            PermissionDenied: When no tenant is active.
        """
        if get_current_tenant_id() is None:
            raise PermissionDenied("no active tenant")
        return super().get_queryset(request)  # type: ignore[no-any-return]

    def log_addition(self, request: HttpRequest, obj: Any, message: Any) -> None:
        """Skip the admin log entry for an addition.

        The admin log keeps object representations regardless of the active tenant, which
        would leak data across tenants. Audit logging belongs to the services.

        Args:
            request: Current request.
            obj: Object that was added.
            message: Change message, ignored.
        """

    def log_change(self, request: HttpRequest, obj: Any, message: Any) -> None:
        """Skip the admin log entry for a change.

        Args:
            request: Current request.
            obj: Object that was changed.
            message: Change message, ignored.
        """

    def log_deletions(self, request: HttpRequest, queryset: Any) -> None:
        """Skip the admin log entries for single and bulk deletions.

        Args:
            request: Current request.
            queryset: Objects that are being deleted.
        """

    def has_module_permission(self, request: HttpRequest) -> bool:
        """Keep the model out of the admin index while no tenant is active.

        Args:
            request: Current request.

        Returns:
            True when a tenant is active and the user may see the module.
        """
        return get_current_tenant_id() is not None and bool(super().has_module_permission(request))

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Refuse the add view while no tenant is active, since it never loads a queryset.

        Args:
            request: Current request.

        Returns:
            True when a tenant is active and the user may add.
        """
        allowed = has_permission(request_principal(request), self.write_permission)
        return (
            allowed
            and get_current_tenant_id() is not None
            and bool(super().has_add_permission(request))
        )

    def has_view_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Require the read permission in addition to Django's view permission.

        Args:
            request: Current request.
            obj: Object being viewed, if any.

        Returns:
            True when the principal may read and Django allows the view.
        """
        allowed = has_permission(request_principal(request), self.read_permission)
        return allowed and bool(super().has_view_permission(request, obj))

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Require the write permission in addition to Django's change permission.

        Args:
            request: Current request.
            obj: Object being changed, if any.

        Returns:
            True when the principal may write and Django allows the change.
        """
        allowed = has_permission(request_principal(request), self.write_permission)
        return allowed and bool(super().has_change_permission(request, obj))

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Require the write permission in addition to Django's delete permission.

        Args:
            request: Current request.
            obj: Object being deleted, if any.

        Returns:
            True when the principal may write and Django allows the delete.
        """
        allowed = has_permission(request_principal(request), self.write_permission)
        return allowed and bool(super().has_delete_permission(request, obj))


class MembershipInline(TabularInline):  # type: ignore[misc]
    """Edit the members of a tenant and their roles on the tenant page."""

    model = Membership
    extra = 0
    raw_id_fields = ("user",)


@admin.register(Tenant)
class TenantAdmin(ModelAdmin):  # type: ignore[misc]
    """Unfold-styled admin listing tenants by name and creation time."""

    list_display = ("name", "created_at")
    search_fields = ("name",)
    inlines = [MembershipInline]
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
