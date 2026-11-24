"""Admin registration for the tenant registry."""

from django.contrib import admin
from unfold.admin import ModelAdmin  # type: ignore[import-untyped]

from .models import Tenant


@admin.register(Tenant)
class TenantAdmin(ModelAdmin):  # type: ignore[misc]
    """Unfold-styled admin listing tenants by name and creation time."""

    list_display = ("name", "created_at")
    search_fields = ("name",)
