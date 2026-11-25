"""Admin registration for billing models."""

import uuid
from typing import Any

from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.forms import ModelForm, ModelMultipleChoiceField
from django.http import HttpRequest
from unfold.admin import TabularInline  # type: ignore[import-untyped]

from tenancy.admin import TenantModelAdmin
from tenancy.permissions import Permission, has_permission, request_principal

from .models import (
    AuditEvent,
    Customer,
    Invoice,
    InvoiceLine,
    InvoiceSeries,
    Payment,
    PaymentAllocation,
    Product,
    TaxRate,
)
from .services import (
    InvalidTransition,
    delete_customer,
    delete_product,
    delete_series,
    delete_tax_rate,
    draft_credit_note,
    issue_invoice,
    save_customer,
    save_product,
    save_series,
    save_tax_rate,
    send_invoice,
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


class InvoiceSeriesForm(ModelForm):  # type: ignore[type-arg]
    """Form that reports a second series of a kind as a field error, as the tenant is no field."""

    class Meta:
        """Form options."""

        model = InvoiceSeries
        fields = ("kind", "prefix")

    def clean_kind(self) -> str:
        """Refuse a kind for which the tenant already has a series.

        Returns:
            The cleaned kind.

        Raises:
            ValidationError: When the tenant already has a series of that kind.
        """
        kind: str = self.cleaned_data["kind"]
        if InvoiceSeries.objects.exclude(pk=self.instance.pk).filter(kind=kind).exists():
            raise ValidationError("A series of this kind already exists.")
        return kind


@admin.register(InvoiceSeries)
class InvoiceSeriesAdmin(TenantModelAdmin):
    """Admin for the active tenant's invoice series, limited to settings:manage holders.

    The counter and the year are shown but never edited, and the kind is fixed once created.
    """

    write_permission = Permission.SETTINGS_MANAGE
    form = InvoiceSeriesForm
    list_display = ("kind", "prefix", "year", "next_number")
    fields = ("kind", "prefix", "year", "next_number")

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        """Make the counter and year read-only, and the kind too once the series exists.

        Args:
            request: Current request.
            obj: Series being changed, or None when adding.

        Returns:
            The names of the read-only fields.
        """
        return ("year", "next_number") + (("kind",) if obj else ())

    def save_model(
        self, request: HttpRequest, obj: InvoiceSeries, form: "ModelForm[Any]", change: bool
    ) -> None:
        """Save a series through the service.

        Args:
            request: Current request.
            obj: Series being saved.
            form: Form that produced the series.
            change: Whether an existing series is being changed.
        """
        save_series(request_principal(request), obj)

    def delete_model(self, request: HttpRequest, obj: InvoiceSeries) -> None:
        """Delete a series through the service.

        Args:
            request: Current request.
            obj: Series being deleted.
        """
        delete_series(request_principal(request), obj)

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet[InvoiceSeries]) -> None:
        """Delete selected series one by one through the service.

        Args:
            request: Current request.
            queryset: Series selected for deletion.
        """
        for series in queryset:
            delete_series(request_principal(request), series)


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


class ReadOnlyInline(TabularInline):  # type: ignore[misc]
    """Tabular inline that shows rows and refuses to add, change or delete any."""

    extra = 0
    can_delete = False

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Refuse adding rows.

        Args:
            request: Current request.
            obj: Parent object, if any.

        Returns:
            False.
        """
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Refuse changing rows.

        Args:
            request: Current request.
            obj: Parent object, if any.

        Returns:
            False.
        """
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Refuse deleting rows.

        Args:
            request: Current request.
            obj: Parent object, if any.

        Returns:
            False.
        """
        return False


class InvoiceLineInline(ReadOnlyInline):
    """The lines of an invoice, read-only."""

    model = InvoiceLine
    fields = ("position", "description", "quantity", "unit_price", "discount_bp", "net", "total")


@admin.register(Invoice)
class InvoiceAdmin(TenantModelAdmin):
    """Read-only admin for the active tenant's invoices; services draft and issue them."""

    list_display = ("id", "kind", "status", "customer", "currency", "total", "issue_date")
    list_filter = ("kind", "status")
    list_select_related = ("customer",)
    inlines = [InvoiceLineInline]
    actions = ["issue_selected_drafts", "create_full_credit_notes", "email_selected_invoices"]

    def has_issue_permission(self, request: HttpRequest) -> bool:
        """Offer the issue action to principals who hold invoices:write.

        Args:
            request: Current request.

        Returns:
            True when the principal may write invoices.
        """
        return has_permission(request_principal(request), Permission.INVOICES_WRITE)

    @admin.action(description="Issue selected drafts", permissions=["issue"])
    def issue_selected_drafts(self, request: HttpRequest, queryset: QuerySet[Invoice]) -> None:
        """Issue each selected draft through the service and report every outcome.

        Drafts are taken ordered by kind and pk.

        Args:
            request: Current request.
            queryset: Invoices selected in the changelist.
        """
        principal = request_principal(request)
        # Always take locks in the same order, so concurrent actions cannot deadlock.
        for invoice in queryset.order_by("kind", "pk"):
            try:
                issued = issue_invoice(principal, invoice)
            except Invoice.DoesNotExist:
                self.message_user(
                    request, f"{invoice.pk}: the invoice no longer exists.", messages.ERROR
                )
            except ValidationError as error:
                self.message_user(
                    request, f"{invoice.pk}: {' '.join(error.messages)}", messages.ERROR
                )
            except InvalidTransition as error:
                self.message_user(request, f"{invoice.pk}: {error}", messages.ERROR)
            else:
                self.message_user(request, f"Issued {issued.reference}.", messages.SUCCESS)

    @admin.action(description="Create full credit notes", permissions=["issue"])
    def create_full_credit_notes(self, request: HttpRequest, queryset: QuerySet[Invoice]) -> None:
        """Draft a full credit note for each selected invoice and report every outcome.

        Invoices are taken ordered by pk. The drafts still have to be issued.

        Args:
            request: Current request.
            queryset: Invoices selected in the changelist.
        """
        principal = request_principal(request)
        for invoice in queryset.order_by("pk"):
            try:
                note = draft_credit_note(principal, invoice)
            except Invoice.DoesNotExist:
                self.message_user(
                    request, f"{invoice.pk}: the invoice no longer exists.", messages.ERROR
                )
            except ValidationError as error:
                self.message_user(
                    request, f"{invoice.pk}: {' '.join(error.messages)}", messages.ERROR
                )
            except InvalidTransition as error:
                self.message_user(request, f"{invoice.pk}: {error}", messages.ERROR)
            else:
                self.message_user(
                    request,
                    f"Drafted credit note {note.pk} for {invoice.reference}.",
                    messages.SUCCESS,
                )

    @admin.action(description="Email selected invoices", permissions=["issue"])
    def email_selected_invoices(self, request: HttpRequest, queryset: QuerySet[Invoice]) -> None:
        """Request the email of each selected invoice and report every outcome.

        Invoices are taken ordered by pk. Each invocation uses a fresh idempotency key per
        invoice, so choosing the action again sends again; the delivery runs in a task.

        Args:
            request: Current request.
            queryset: Invoices selected in the changelist.
        """
        principal = request_principal(request)
        for invoice in queryset.order_by("pk"):
            try:
                send_invoice(principal, invoice, uuid.uuid4().hex)
            except Invoice.DoesNotExist:
                self.message_user(
                    request, f"{invoice.pk}: the invoice no longer exists.", messages.ERROR
                )
            except ValidationError as error:
                self.message_user(
                    request, f"{invoice.pk}: {' '.join(error.messages)}", messages.ERROR
                )
            except InvalidTransition as error:
                self.message_user(request, f"{invoice.pk}: {error}", messages.ERROR)
            else:
                self.message_user(
                    request, f"Email requested for {invoice.reference}.", messages.SUCCESS
                )

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Refuse adding invoices, which only the services create.

        Args:
            request: Current request.

        Returns:
            False.
        """
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Refuse changing invoices, so stored amounts cannot drift from the services.

        Args:
            request: Current request.
            obj: Object being changed, if any.

        Returns:
            False.
        """
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Refuse deleting invoices, which only the services remove.

        Args:
            request: Current request.
            obj: Object being deleted, if any.

        Returns:
            False.
        """
        return False


class PaymentAllocationInline(ReadOnlyInline):
    """The allocations of a payment, read-only."""

    model = PaymentAllocation
    fields = ("invoice", "amount")


@admin.register(Payment)
class PaymentAdmin(TenantModelAdmin):
    """Read-only admin for the active tenant's payments; the services record them."""

    list_display = ("value_date", "amount", "currency", "source", "reference")
    list_filter = ("source", "currency")
    search_fields = ("reference",)
    inlines = [PaymentAllocationInline]

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Refuse adding payments, which only record_payment creates.

        Args:
            request: Current request.

        Returns:
            False.
        """
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Refuse changing payments, so the stored amounts stay as recorded.

        Args:
            request: Current request.
            obj: Object being changed, if any.

        Returns:
            False.
        """
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Refuse deleting payments, which would unbalance the invoices they settle.

        Args:
            request: Current request.
            obj: Object being deleted, if any.

        Returns:
            False.
        """
        return False
