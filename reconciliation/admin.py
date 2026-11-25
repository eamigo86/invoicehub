"""Admin registration for reconciliation models."""

from typing import Any

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.forms import ModelChoiceField, ModelForm
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import SafeString

from tenancy.admin import TenantModelAdmin
from tenancy.permissions import Permission, request_principal

from . import services
from .models import BankAccount, BankStatementImport, BankTransaction
from .services import create_import, save_bank_account


class BankAccountForm(ModelForm):  # type: ignore[type-arg]
    """Form that reports a duplicate name as a field error, since the tenant is not a form field."""

    class Meta:
        """Form options."""

        model = BankAccount
        fields = "__all__"

    def clean_name(self) -> str:
        """Refuse a name already used by another account of the tenant.

        Returns:
            The cleaned name.

        Raises:
            ValidationError: When the name is taken.
        """
        name: str = self.cleaned_data["name"]
        if BankAccount.objects.exclude(pk=self.instance.pk).filter(name=name).exists():
            raise ValidationError("A bank account with this name already exists.")
        return name


@admin.register(BankAccount)
class BankAccountAdmin(TenantModelAdmin):
    """Admin for the active tenant's bank accounts, saved through the service and never deleted."""

    read_permission = Permission.INVOICES_READ
    write_permission = Permission.RECONCILIATION_WRITE
    form = BankAccountForm
    list_display = ("name", "account_identifier", "currency")
    search_fields = ("name", "account_identifier")

    def save_model(
        self, request: HttpRequest, obj: BankAccount, form: "ModelForm[Any]", change: bool
    ) -> None:
        """Save a bank account through the service.

        Args:
            request: Current request.
            obj: Account being saved.
            form: Form that produced the account.
            change: Whether an existing account is being changed.
        """
        save_bank_account(request_principal(request), obj)

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Refuse deleting accounts, which imports and transactions reference.

        Args:
            request: Current request.
            obj: Object being deleted, if any.

        Returns:
            False.
        """
        return False


class BankStatementUploadForm(ModelForm):  # type: ignore[type-arg]
    """The upload form: the account and the CSV, nothing else.

    Attributes:
        bank_account: Account of the tenant the statement belongs to.
    """

    bank_account = ModelChoiceField(queryset=BankAccount._base_manager.none())

    class Meta:
        """Form options."""

        model = BankStatementImport
        fields = ("bank_account", "file")

    def clean_file(self) -> Any:
        """Refuse a file over the size limit before any of it is read.

        Returns:
            The uploaded file.

        Raises:
            ValidationError: When the file is larger than the statement size limit.
        """
        file = self.cleaned_data["file"]
        if file.size is not None and file.size > services.MAX_STATEMENT_BYTES:
            raise ValidationError(
                f"The file is too large: the limit is {services.MAX_STATEMENT_BYTES} bytes."
            )
        return file

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Scope the account choices to the active tenant.

        Args:
            *args: Positional arguments forwarded to ModelForm.
            **kwargs: Keyword arguments forwarded to ModelForm.
        """
        super().__init__(*args, **kwargs)
        if "bank_account" in self.fields:
            self.fields["bank_account"].queryset = BankAccount.objects.all()  # type: ignore[attr-defined]


@admin.register(BankStatementImport)
class BankStatementImportAdmin(TenantModelAdmin):
    """Admin that uploads bank statements and shows how each import went; imports never change.

    The add form creates the import through create_import, which is idempotent and may hand
    back an earlier import for the same file, so the admin redirects to whatever it returned.
    """

    read_permission = Permission.INVOICES_READ
    write_permission = Permission.RECONCILIATION_WRITE
    form = BankStatementUploadForm
    list_display = ("bank_account", "status", "rows_read", "duplicates", "imported", "created_at")
    list_filter = ("status",)
    list_select_related = ("bank_account",)
    readonly_fields = ("transactions",)

    def get_fields(self, request: HttpRequest, obj: Any = None) -> list[str]:
        """Pick the upload fields when adding and the outcome fields when viewing.

        Args:
            request: Current request.
            obj: Import being viewed, or None on the add form.

        Returns:
            The field names to show.
        """
        if obj is None:
            return ["bank_account", "file"]
        return [
            "bank_account",
            "status",
            "rows_read",
            "duplicates",
            "imported",
            "error",
            "mapping",
            "created_at",
            "completed_at",
            "transactions",
        ]

    @admin.display(description="Transactions")
    def transactions(self, obj: BankStatementImport) -> SafeString:
        """Link the transaction list filtered by this import.

        Args:
            obj: Import being viewed.

        Returns:
            A link showing how many transactions the import brought in.
        """
        url = reverse("admin:reconciliation_banktransaction_changelist")
        return format_html(
            '<a href="{}?statement_import__id__exact={}">{} transactions</a>',
            url,
            obj.pk,
            obj.transactions.count(),
        )

    def save_model(
        self, request: HttpRequest, obj: BankStatementImport, form: "ModelForm[Any]", change: bool
    ) -> None:
        """Create the import through the service instead of saving the unsaved form instance.

        The unsaved instance is never written. It only takes over the primary key of the stored
        import, which may be an earlier one, so that response_add can redirect to the right page.

        Args:
            request: Current request.
            obj: Unsaved instance built by the form.
            form: Validated upload form.
            change: Whether an existing import is being changed, which never happens.
        """
        content = form.cleaned_data["file"].read()
        stored = create_import(
            request_principal(request), form.cleaned_data["bank_account"], content
        )
        obj.pk = stored.pk

    def response_add(
        self, request: HttpRequest, obj: BankStatementImport, post_url_continue: str | None = None
    ) -> HttpResponse:
        """Redirect to the import page, whether the import is new or an earlier one.

        Args:
            request: Current request.
            obj: Instance that took over the primary key of the stored import.
            post_url_continue: Ignored.

        Returns:
            A redirect to the import's page.
        """
        return HttpResponseRedirect(
            reverse("admin:reconciliation_bankstatementimport_change", args=[obj.pk])
        )

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Refuse changing imports, so the page is a read-only detail.

        Args:
            request: Current request.
            obj: Object being changed, if any.

        Returns:
            False.
        """
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Refuse deleting imports, whose transactions keep referencing them.

        Args:
            request: Current request.
            obj: Object being deleted, if any.

        Returns:
            False.
        """
        return False


@admin.register(BankTransaction)
class BankTransactionAdmin(TenantModelAdmin):
    """Read-only admin for the active tenant's bank transactions; imports create them."""

    read_permission = Permission.INVOICES_READ
    write_permission = Permission.RECONCILIATION_WRITE
    list_display = ("value_date", "amount_display", "description", "payer", "bank_account")
    list_filter = ("bank_account", "statement_import")
    list_select_related = ("bank_account",)
    search_fields = ("description", "payer")

    @admin.display(description="Amount", ordering="amount")
    def amount_display(self, obj: BankTransaction) -> str:
        """Show the amount in the account currency with its sign, using integer math.

        Args:
            obj: Transaction being listed.

        Returns:
            For example -12.50 USD for a debit of 1250 cents.
        """
        whole, cents = divmod(abs(obj.amount), 100)
        sign = "-" if obj.amount < 0 else ""
        return f"{sign}{whole:,}.{cents:02d} {obj.bank_account.currency}"

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Refuse adding transactions, which only the import creates.

        Args:
            request: Current request.

        Returns:
            False.
        """
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Refuse changing transactions, so they stay as the bank reported them.

        Args:
            request: Current request.
            obj: Object being changed, if any.

        Returns:
            False.
        """
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Refuse deleting transactions, which later matching depends on.

        Args:
            request: Current request.
            obj: Object being deleted, if any.

        Returns:
            False.
        """
        return False
