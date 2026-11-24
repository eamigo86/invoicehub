"""Admin registration for billing models."""

from typing import Any

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.forms import ModelForm
from django.http import HttpRequest

from tenancy.admin import TenantModelAdmin
from tenancy.permissions import Permission, request_principal

from .models import Customer, TaxRate
from .services import delete_customer, delete_tax_rate, save_customer, save_tax_rate


@admin.register(Customer)
class CustomerAdmin(TenantModelAdmin):
    """Admin for the active tenant's customers; every write goes through the services."""

    list_display = ("name", "tax_id", "emails", "city", "state")
    search_fields = ("name", "tax_id", "emails")
    fields = (
        "name",
        "tax_id",
        "emails",
        "address_line1",
        "address_line2",
        "city",
        "state",
        "postal_code",
        "country",
    )

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


class TaxRateForm(ModelForm):  # type: ignore[type-arg]
    """Form that reports a duplicate name as a field error, since the tenant is not a form field."""

    class Meta:
        """Form options."""

        model = TaxRate
        fields = ("name", "rate_bp")

    def clean_name(self) -> str:
        """Refuse a name already used by another tax rate of the tenant.

        Returns:
            The cleaned name.

        Raises:
            ValidationError: When the name is taken.
        """
        name: str = self.cleaned_data["name"]
        if TaxRate.objects.exclude(pk=self.instance.pk).filter(name=name).exists():
            raise ValidationError("A tax rate with this name already exists.")
        return name


@admin.register(TaxRate)
class TaxRateAdmin(TenantModelAdmin):
    """Admin for the active tenant's tax rates, limited to settings:manage holders."""

    write_permission = Permission.SETTINGS_MANAGE
    form = TaxRateForm
    list_display = ("name", "rate_bp")
    search_fields = ("name",)

    def save_model(
        self, request: HttpRequest, obj: TaxRate, form: "ModelForm[Any]", change: bool
    ) -> None:
        """Save a tax rate through the service.

        Args:
            request: Current request.
            obj: Tax rate being saved.
            form: Form that produced the tax rate.
            change: Whether an existing tax rate is being changed.
        """
        save_tax_rate(request_principal(request), obj)

    def delete_model(self, request: HttpRequest, obj: TaxRate) -> None:
        """Delete a tax rate through the service.

        Args:
            request: Current request.
            obj: Tax rate being deleted.
        """
        delete_tax_rate(request_principal(request), obj)

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet[TaxRate]) -> None:
        """Delete selected tax rates one by one through the service.

        Args:
            request: Current request.
            queryset: Tax rates selected for deletion.
        """
        for tax_rate in queryset:
            delete_tax_rate(request_principal(request), tax_rate)
