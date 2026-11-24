"""Admin registration for billing models."""

from typing import Any

from django.contrib import admin
from django.db.models import QuerySet
from django.forms import ModelForm
from django.http import HttpRequest

from tenancy.admin import TenantModelAdmin
from tenancy.permissions import request_principal

from .models import Customer
from .services import delete_customer, save_customer


@admin.register(Customer)
class CustomerAdmin(TenantModelAdmin):
    """Admin for the active tenant's customers; every write goes through the services."""

    list_display = ("name", "tax_id", "email")
    search_fields = ("name",)

    def save_model(
        self, request: HttpRequest, obj: Customer, form: "ModelForm[Any]", change: bool
    ) -> None:
        """Save a customer through the service.

        Args:
            request: Current request.
            obj: Customer being saved.
            form: Form that produced the customer.
            change: Whether an existing customer is being changed.
        """
        save_customer(request_principal(request), obj)

    def delete_model(self, request: HttpRequest, obj: Customer) -> None:
        """Delete a customer through the service.

        Args:
            request: Current request.
            obj: Customer being deleted.
        """
        delete_customer(request_principal(request), obj)

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet[Customer]) -> None:
        """Delete selected customers one by one through the service.

        Args:
            request: Current request.
            queryset: Customers selected for deletion.
        """
        for customer in queryset:
            delete_customer(request_principal(request), customer)
