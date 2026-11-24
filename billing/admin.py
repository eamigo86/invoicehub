"""Admin registration for billing models."""

from typing import Any

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.forms import ModelForm, ModelMultipleChoiceField
from django.http import HttpRequest

from tenancy.admin import TenantModelAdmin
from tenancy.permissions import Permission, request_principal

from .models import AuditEvent, Customer, Product, TaxRate
from .services import (
    delete_customer,
    delete_product,
    delete_tax_rate,
    save_customer,
    save_product,
    save_tax_rate,
)


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


class ProductForm(ModelForm):  # type: ignore[type-arg]
    """Product form whose taxes field edits the default tax links, which the model form skips.

    Attributes:
        taxes: Tax rates of the tenant to link as default taxes.
    """

    taxes = ModelMultipleChoiceField(queryset=TaxRate._base_manager.none(), required=False)

    class Meta:
        """Form options."""

        model = Product
        fields = ("name", "description", "unit_price")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Scope the taxes choices to the active tenant and preselect the current links.

        Args:
            *args: Positional arguments forwarded to ModelForm.
            **kwargs: Keyword arguments forwarded to ModelForm.
        """
        super().__init__(*args, **kwargs)
        self.fields["taxes"].queryset = TaxRate.objects.all()  # type: ignore[attr-defined]
        if self.instance.pk:
            self.initial["taxes"] = [rate.pk for rate in self.instance.default_taxes.all()]


@admin.register(Product)
class ProductAdmin(TenantModelAdmin):
    """Admin for the active tenant's products; every write goes through the services."""

    form = ProductForm
    list_display = ("name", "unit_price")
    search_fields = ("name",)
    fields = ("name", "description", "unit_price", "taxes")

    def save_model(
        self, request: HttpRequest, obj: Product, form: "ModelForm[Any]", change: bool
    ) -> None:
        """Save a product and its default taxes through the service.

        Args:
            request: Current request.
            obj: Product being saved.
            form: Form that produced the product, carrying the chosen taxes.
            change: Whether an existing product is being changed.
        """
        save_product(request_principal(request), obj, form.cleaned_data["taxes"])

    def delete_model(self, request: HttpRequest, obj: Product) -> None:
        """Delete a product through the service.

        Args:
            request: Current request.
            obj: Product being deleted.
        """
        delete_product(request_principal(request), obj)

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet[Product]) -> None:
        """Delete selected products one by one through the service.

        Args:
            request: Current request.
            queryset: Products selected for deletion.
        """
        for product in queryset:
            delete_product(request_principal(request), product)


@admin.register(AuditEvent)
class AuditEventAdmin(TenantModelAdmin):
    """Read-only admin for the active tenant's audit log; no one adds, changes or deletes events."""

    list_display = ("created_at", "actor", "action", "entity", "entity_id")
    list_filter = ("action", "actor_kind")
    list_select_related = ("actor_user", "actor_token")

    @admin.display(description="Actor")
    def actor(self, obj: AuditEvent) -> str:
        """Name who acted, falling back to the kind when the user or token is gone.

        Args:
            obj: Event being listed.

        Returns:
            The username or token name, or the actor kind.
        """
        return str(obj.actor_user or obj.actor_token or obj.get_actor_kind_display())

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Refuse adding events, which only record_event creates.

        Args:
            request: Current request.

        Returns:
            False.
        """
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Refuse changing events, so the log stays as it was written.

        Args:
            request: Current request.
            obj: Object being changed, if any.

        Returns:
            False.
        """
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Refuse deleting events, so the log is append-only.

        Args:
            request: Current request.
            obj: Object being deleted, if any.

        Returns:
            False.
        """
        return False
