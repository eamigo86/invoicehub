"""Admin registration for reconciliation models."""

from collections.abc import Callable
from typing import Any

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db.models import Case, Count, Q, QuerySet, When
from django.forms import ModelChoiceField, ModelForm
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString

from billing.models import Invoice
from billing.services import InvalidTransition
from tenancy.admin import TenantModelAdmin
from tenancy.permissions import Permission, has_permission, request_principal

from . import services
from .models import (
    BankAccount,
    BankStatementImport,
    BankTransaction,
    MatchProposal,
    ReconciliationRun,
)
from .services import (
    accept_proposal,
    create_import,
    reassign_proposal,
    reject_proposal,
    save_bank_account,
    undo_proposal,
)


def format_cents(amount: int, currency: str) -> str:
    """Show cents as a signed amount with its currency, using integer math.

    Args:
        amount: Signed amount in cents.
        currency: Currency code to print after the amount.

    Returns:
        For example -12.50 USD for a debit of 1250 cents.
    """
    whole, cents = divmod(abs(amount), 100)
    sign = "-" if amount < 0 else ""
    return f"{sign}{whole:,}.{cents:02d} {currency}"


def format_percent(part: int, whole: int) -> str:
    """Show a share as a percentage with one decimal, rounded half up, without floats.

    Args:
        part: Numerator.
        whole: Denominator.

    Returns:
        For example 33.3%, or a dash when the denominator is zero.
    """
    if whole == 0:
        return "-"
    tenths = (part * 1000 + whole // 2) // whole
    return f"{tenths // 10}.{tenths % 10}%"


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
        return format_cents(obj.amount, obj.bank_account.currency)

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


@admin.register(ReconciliationRun)
class ReconciliationRunAdmin(TenantModelAdmin):
    """Admin that starts reconciliation runs and shows the metrics of each; runs never change.

    The add form has no fields: run_reconciliation may hand back the run that is already
    pending, so the admin redirects to whatever it returned.
    """

    read_permission = Permission.INVOICES_READ
    write_permission = Permission.RECONCILIATION_WRITE
    list_display = (
        "created_at",
        "status",
        "requested_by",
        "engine_version",
        "transactions_considered",
        "invoices_considered",
        "auto_count",
        "suggested_count",
        "ambiguous_count",
        "applied_count",
        "unmatched_transactions",
        "unmatched_invoices",
        "duration",
        "percent_auto",
        "percent_suggested",
        "percent_unmatched",
        "suggestion_acceptance",
    )
    list_select_related = ("requested_by",)
    readonly_fields = ("proposals_link",)

    def get_queryset(self, request: HttpRequest) -> QuerySet[ReconciliationRun]:
        """Annotate each run with its decided suggested proposals, accepted and rejected.

        Applied and reassigned count as accepted. Pending and undone proposals are not decided
        by the review, so they count as neither.

        Args:
            request: Current request.

        Returns:
            The active tenant's runs with suggested_accepted and suggested_rejected.
        """
        suggested = Q(proposals__engine_status=MatchProposal.EngineStatus.SUGGESTED)
        accepted = [MatchProposal.State.APPLIED, MatchProposal.State.REASSIGNED]
        queryset: QuerySet[ReconciliationRun] = super().get_queryset(request)
        return queryset.annotate(
            suggested_accepted=Count(
                "proposals", filter=suggested & Q(proposals__state__in=accepted)
            ),
            suggested_rejected=Count(
                "proposals", filter=suggested & Q(proposals__state=MatchProposal.State.REJECTED)
            ),
        )

    def get_fields(self, request: HttpRequest, obj: Any = None) -> list[str]:
        """Show no field on the add form and the outcome fields on the detail page.

        Args:
            request: Current request.
            obj: Run being viewed, or None on the add form.

        Returns:
            The field names to show.
        """
        if obj is None:
            return []
        return [
            "status",
            "requested_by",
            "engine_version",
            "config",
            "error",
            "transactions_considered",
            "invoices_considered",
            "created_at",
            "completed_at",
            "proposals_link",
        ]

    @admin.display(description="Proposals")
    def proposals_link(self, obj: ReconciliationRun) -> SafeString:
        """Link the proposal list filtered by this run.

        Args:
            obj: Run being viewed.

        Returns:
            A link to the run's proposals.
        """
        url = reverse("admin:reconciliation_matchproposal_changelist")
        return format_html('<a href="{}?run__id__exact={}">Proposals of this run</a>', url, obj.pk)

    @admin.display(description="Duration")
    def duration(self, obj: ReconciliationRun) -> str:
        """Show the wall time in seconds with milliseconds.

        Args:
            obj: Run being listed.

        Returns:
            For example 1.250 s, or a dash while the run has no duration.
        """
        if obj.duration_ms is None:
            return "-"
        return f"{obj.duration_ms // 1000}.{obj.duration_ms % 1000:03d} s"

    @admin.display(description="Auto")
    def percent_auto(self, obj: ReconciliationRun) -> str:
        """Show the auto proposals over the transactions considered.

        Args:
            obj: Run being listed.

        Returns:
            The percentage, or a dash when no transaction was considered.
        """
        return format_percent(obj.auto_count, obj.transactions_considered)

    @admin.display(description="Suggested")
    def percent_suggested(self, obj: ReconciliationRun) -> str:
        """Show the suggested proposals over the transactions considered.

        Args:
            obj: Run being listed.

        Returns:
            The percentage, or a dash when no transaction was considered.
        """
        return format_percent(obj.suggested_count, obj.transactions_considered)

    @admin.display(description="Unmatched")
    def percent_unmatched(self, obj: ReconciliationRun) -> str:
        """Show the unmatched transactions over the transactions considered.

        Args:
            obj: Run being listed.

        Returns:
            The percentage, or a dash when no transaction was considered.
        """
        return format_percent(obj.unmatched_transactions, obj.transactions_considered)

    @admin.display(description="Suggestions accepted")
    def suggestion_acceptance(self, obj: ReconciliationRun) -> str:
        """Show how many decided suggestions were applied or reassigned instead of rejected.

        A rate near 100% means the auto threshold is too high.

        Args:
            obj: Run being listed, annotated by get_queryset.

        Returns:
            The percentage, or a dash while no suggestion has been decided.
        """
        accepted: int = obj.suggested_accepted  # type: ignore[attr-defined]
        rejected: int = obj.suggested_rejected  # type: ignore[attr-defined]
        return format_percent(accepted, accepted + rejected)

    def save_model(
        self, request: HttpRequest, obj: ReconciliationRun, form: "ModelForm[Any]", change: bool
    ) -> None:
        """Request the run through the service instead of saving the unsaved instance.

        The unsaved instance is never written. It only takes over the primary key of the run the
        service returned, which may be one that was already pending.

        Args:
            request: Current request.
            obj: Unsaved instance built by the form.
            form: Validated, empty form.
            change: Whether an existing run is being changed, which never happens.
        """
        obj.pk = services.run_reconciliation(request_principal(request)).pk

    def response_add(
        self, request: HttpRequest, obj: ReconciliationRun, post_url_continue: str | None = None
    ) -> HttpResponse:
        """Redirect to the run page, whether the run is new or was already pending.

        Args:
            request: Current request.
            obj: Instance that took over the primary key of the run.
            post_url_continue: Ignored.

        Returns:
            A redirect to the run's page.
        """
        return HttpResponseRedirect(
            reverse("admin:reconciliation_reconciliationrun_change", args=[obj.pk])
        )

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Refuse changing runs, so the page is a read-only detail.

        Args:
            request: Current request.
            obj: Object being changed, if any.

        Returns:
            False.
        """
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Refuse deleting runs, which their proposals keep referencing.

        Args:
            request: Current request.
            obj: Object being deleted, if any.

        Returns:
            False.
        """
        return False


class ReassignForm(forms.Form):
    """Choose the invoices that the transactions of a pending proposal should pay.

    Attributes:
        invoices: Payable invoices of the tenant that still have a balance.
    """

    invoices = forms.ModelMultipleChoiceField(queryset=Invoice._base_manager.none())

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Scope the invoice choices to the active tenant's payable invoices with a balance.

        Args:
            *args: Positional arguments forwarded to Form.
            **kwargs: Keyword arguments forwarded to Form.
        """
        super().__init__(*args, **kwargs)
        # ponytail: one balance query per invoice, like the run task. Annotate the balance when
        # tenants have thousands of open invoices.
        balances = {
            invoice.pk: balance
            for invoice in Invoice.objects.filter(
                kind=Invoice.Kind.INVOICE, status__in=Invoice.PAYABLE_STATUSES
            )
            if (balance := invoice.balance_due()) > 0
        }
        field: Any = self.fields["invoices"]
        field.queryset = Invoice.objects.filter(pk__in=balances).select_related("customer")
        field.label_from_instance = lambda invoice: (
            f"{invoice.reference} - {invoice.customer} - "
            f"{format_cents(balances[invoice.pk], invoice.currency)} due"
        )


@admin.register(MatchProposal)
class MatchProposalAdmin(TenantModelAdmin):
    """Read-only review queue of match proposals, decided through actions and the reassign page."""

    read_permission = Permission.INVOICES_READ
    write_permission = Permission.RECONCILIATION_WRITE
    list_display = (
        "run",
        "kind",
        "engine_status",
        "state",
        "score",
        "residual_display",
        "transactions_summary",
        "invoices_summary",
        "decided_by",
        "decided_at",
    )
    list_filter = ("state", "engine_status", "kind", "run")
    list_select_related = ("run", "decided_by")
    ordering = (Case(When(state=MatchProposal.State.PENDING, then=0), default=1), "-created_at")
    actions = ["accept_selected", "reject_selected", "undo_selected"]
    readonly_fields = (
        "residual_display",
        "breakdown_display",
        "transactions_detail",
        "invoices_detail",
        "reassign_link",
    )

    def get_queryset(self, request: HttpRequest) -> QuerySet[MatchProposal]:
        """Prefetch the linked rows that the summaries print.

        Args:
            request: Current request.

        Returns:
            The active tenant's proposals.
        """
        queryset: QuerySet[MatchProposal] = super().get_queryset(request)
        return queryset.prefetch_related("transactions__bank_account", "invoices")

    def get_fields(self, request: HttpRequest, obj: Any = None) -> list[str]:
        """List the detail fields, with the reassign link only for those who may use it.

        Args:
            request: Current request.
            obj: Proposal being viewed.

        Returns:
            The field names to show.
        """
        fields = [
            "run",
            "kind",
            "engine_status",
            "state",
            "score",
            "breakdown_display",
            "residual_display",
            "transactions_detail",
            "invoices_detail",
            "decided_by",
            "decided_at",
            "created_at",
        ]
        if self.has_reassign_permission(request):
            fields.append("reassign_link")
        return fields

    def _currency(self, obj: MatchProposal) -> str:
        """Pick the currency of a proposal from its first transaction's account.

        Args:
            obj: Proposal being shown.

        Returns:
            The currency code, empty when the proposal has no transaction.
        """
        first = next(iter(obj.transactions.all()), None)
        return str(first.bank_account.currency) if first else ""

    @admin.display(description="Residual", ordering="residual")
    def residual_display(self, obj: MatchProposal) -> str:
        """Show the residual in the currency of the transactions.

        Args:
            obj: Proposal being shown.

        Returns:
            For example -12.50 USD.
        """
        return format_cents(obj.residual, self._currency(obj))

    @admin.display(description="Transactions")
    def transactions_summary(self, obj: MatchProposal) -> str:
        """Summarize the linked transactions by amount, at most three.

        Args:
            obj: Proposal being listed.

        Returns:
            The first amounts and how many more there are.
        """
        rows = list(obj.transactions.all())
        shown = ", ".join(format_cents(t.amount, t.bank_account.currency) for t in rows[:3])
        return shown + (f" +{len(rows) - 3} more" if len(rows) > 3 else "")

    @admin.display(description="Invoices")
    def invoices_summary(self, obj: MatchProposal) -> str:
        """Summarize the linked invoices by reference, at most three.

        Args:
            obj: Proposal being listed.

        Returns:
            The first references and how many more there are.
        """
        rows = list(obj.invoices.all())
        shown = ", ".join(str(i.reference or i.pk) for i in rows[:3])
        return shown + (f" +{len(rows) - 3} more" if len(rows) > 3 else "")

    @admin.display(description="Score breakdown")
    def breakdown_display(self, obj: MatchProposal) -> SafeString:
        """List the score of each signal.

        Args:
            obj: Proposal being viewed.

        Returns:
            One line per signal such as amount: 1.00.
        """
        lines = format_html_join(
            "", "<li>{}: {}</li>", ((k, f"{v:.2f}") for k, v in obj.breakdown.items())
        )
        return format_html("<ul>{}</ul>", lines)

    @admin.display(description="Linked transactions")
    def transactions_detail(self, obj: MatchProposal) -> SafeString:
        """List the linked transactions with links to their pages.

        Args:
            obj: Proposal being viewed.

        Returns:
            One linked line per transaction.
        """
        return format_html(
            "<ul>{}</ul>",
            format_html_join(
                "",
                '<li><a href="{}">{} {}</a></li>',
                (
                    (
                        reverse("admin:reconciliation_banktransaction_change", args=[t.pk]),
                        format_cents(t.amount, t.bank_account.currency),
                        t.description,
                    )
                    for t in obj.transactions.all()
                ),
            ),
        )

    @admin.display(description="Linked invoices")
    def invoices_detail(self, obj: MatchProposal) -> SafeString:
        """List the linked invoices with links to their pages.

        Args:
            obj: Proposal being viewed.

        Returns:
            One linked line per invoice.
        """
        return format_html(
            "<ul>{}</ul>",
            format_html_join(
                "",
                '<li><a href="{}">{}</a></li>',
                (
                    (reverse("admin:billing_invoice_change", args=[i.pk]), i.reference or i.pk)
                    for i in obj.invoices.all()
                ),
            ),
        )

    @admin.display(description="Reassign")
    def reassign_link(self, obj: MatchProposal) -> SafeString | str:
        """Link the reassign page of a pending proposal.

        Args:
            obj: Proposal being viewed.

        Returns:
            A link, or a dash when the proposal is no longer pending.
        """
        if obj.state != MatchProposal.State.PENDING:
            return "-"
        url = reverse("admin:reconciliation_matchproposal_reassign", args=[obj.pk])
        return format_html('<a href="{}">Pay other invoices with these transactions</a>', url)

    def has_reassign_permission(self, request: HttpRequest) -> bool:
        """Offer the review actions and the reassign page to principals who write reconciliation.

        Args:
            request: Current request.

        Returns:
            True when the principal holds reconciliation:write.
        """
        return has_permission(request_principal(request), Permission.RECONCILIATION_WRITE)

    def _decide(
        self,
        request: HttpRequest,
        queryset: QuerySet[MatchProposal],
        service: Callable[[Any, MatchProposal], object],
        past: str,
    ) -> None:
        """Apply a review service to each selected proposal and report every outcome.

        Proposals are taken ordered by pk so concurrent actions lock in the same order.

        Args:
            request: Current request.
            queryset: Proposals selected in the changelist.
            service: Service called with the principal and each proposal.
            past: Past tense of the verb for the summary, such as Accepted.
        """
        principal = request_principal(request)
        done = 0
        for proposal in queryset.order_by("pk"):
            try:
                service(principal, proposal)
            except ValidationError as error:
                self.message_user(
                    request, f"{proposal.pk}: {' '.join(error.messages)}", messages.ERROR
                )
            except (InvalidTransition, PermissionDenied, ObjectDoesNotExist) as error:
                reason = str(error) or "the action was refused."
                self.message_user(request, f"{proposal.pk}: {reason}", messages.ERROR)
            else:
                done += 1
        if done:
            self.message_user(
                request, f"{past} {done} proposal{'s' if done > 1 else ''}.", messages.SUCCESS
            )

    @admin.action(description="Accept selected proposals", permissions=["reassign"])
    def accept_selected(self, request: HttpRequest, queryset: QuerySet[MatchProposal]) -> None:
        """Accept each selected proposal, recording the payments.

        Args:
            request: Current request.
            queryset: Proposals selected in the changelist.
        """
        self._decide(request, queryset, accept_proposal, "Accepted")

    @admin.action(description="Reject selected proposals", permissions=["reassign"])
    def reject_selected(self, request: HttpRequest, queryset: QuerySet[MatchProposal]) -> None:
        """Reject each selected proposal.

        Args:
            request: Current request.
            queryset: Proposals selected in the changelist.
        """
        self._decide(request, queryset, reject_proposal, "Rejected")

    @admin.action(description="Undo selected proposals", permissions=["reassign"])
    def undo_selected(self, request: HttpRequest, queryset: QuerySet[MatchProposal]) -> None:
        """Undo each selected applied proposal by reversing its payments.

        Args:
            request: Current request.
            queryset: Proposals selected in the changelist.
        """
        self._decide(request, queryset, undo_proposal, "Undone")

    def get_urls(self) -> list[Any]:
        """Add the reassign page ahead of the default admin URLs.

        Returns:
            The URL patterns.
        """
        reassign = path(
            "<path:object_id>/reassign/",
            self.admin_site.admin_view(self.reassign_view),
            name="reconciliation_matchproposal_reassign",
        )
        return [reassign, *super().get_urls()]

    def reassign_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        """Show the reassign form of a pending proposal and apply the chosen invoices.

        Args:
            request: Current request.
            object_id: Primary key of the proposal.

        Returns:
            The form, or a redirect to the proposal page after a success or a refusal because
            the proposal is not pending.

        Raises:
            PermissionDenied: When the principal may not review proposals.
            Http404: When the proposal is not one of the active tenant's.
        """
        proposal = self.get_object(request, object_id)
        if not self.has_reassign_permission(request) or not self.has_view_permission(request):
            raise PermissionDenied
        if proposal is None:
            raise Http404("No such proposal.")
        detail = HttpResponseRedirect(
            reverse("admin:reconciliation_matchproposal_change", args=[proposal.pk])
        )
        if proposal.state != MatchProposal.State.PENDING:
            self.message_user(request, "Only a pending proposal can be reassigned.", messages.ERROR)
            return detail
        form = ReassignForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            try:
                reassign_proposal(
                    request_principal(request), proposal, list(form.cleaned_data["invoices"])
                )
            except ValidationError as error:
                self.message_user(request, " ".join(error.messages), messages.ERROR)
            except (InvalidTransition, PermissionDenied, ObjectDoesNotExist) as error:
                self.message_user(request, str(error) or "Refused.", messages.ERROR)
            else:
                self.message_user(request, "Reassigned the proposal.", messages.SUCCESS)
                return detail
        context = {
            **self.admin_site.each_context(request),
            "opts": self.opts,
            "title": "Reassign proposal",
            "form": form,
            "proposal": proposal,
        }
        # Rendered here, because the choices query the database and the tenant is only active
        # while the view runs.
        return TemplateResponse(request, "admin/reconciliation/reassign.html", context).render()

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Refuse adding proposals, which only runs create.

        Args:
            request: Current request.

        Returns:
            False.
        """
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Refuse changing proposals, which only the review services decide.

        Args:
            request: Current request.
            obj: Object being changed, if any.

        Returns:
            False.
        """
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Refuse deleting proposals, which the audit trail refers to.

        Args:
            request: Current request.
            obj: Object being deleted, if any.

        Returns:
            False.
        """
        return False
