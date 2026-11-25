"""The one module that wraps remitmatch, so an API change in the alpha touches a single file."""

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

import remitmatch

if TYPE_CHECKING:
    from billing.models import Invoice
    from reconciliation.models import BankTransaction


def engine_version() -> str:
    """Return the version of the installed matching engine.

    Returns:
        The remitmatch version string.
    """
    return remitmatch.__version__


def default_config() -> dict[str, object]:
    """Return the engine default match configuration as plain JSON values.

    Returns:
        The tolerance, window, group size, thresholds, margin and signal weights.
    """
    config = remitmatch.MatchConfig()
    return {
        "amount_tolerance_minor": config.amount_tolerance_minor,
        "date_window_days": config.date_window_days,
        "max_group_size": config.max_group_size,
        "auto_threshold": config.auto_threshold,
        "suggest_threshold": config.suggest_threshold,
        "ambiguity_margin": config.ambiguity_margin,
        "weights": config.weights,
    }


def match(
    invoices: Iterable[tuple["Invoice", int]],
    transactions: Iterable["BankTransaction"],
    config: Mapping[str, object],
    scope: str,
) -> remitmatch.ReconciliationResult:
    """Convert InvoiceHub rows to engine inputs and run the matching engine.

    The caller does the queries: it passes the invoices with the balance due of each, and
    transactions whose bank account is already loaded, so this function never touches the
    database. Ids are the primary keys as text. Errors of the engine propagate: ValueError and
    TypeError are bugs of the caller, OverflowError is bad data.

    Args:
        invoices: Open invoices, each with its balance due in cents, which must be positive.
        transactions: Credit transactions that no proposal holds, with positive amounts.
        config: Match configuration as stored on a run, the output of default_config.
        scope: Scope of every row, which is the tenant id, so tenants never match each other.

    Returns:
        The engine result, with matches and the ids left unmatched on each side.
    """
    engine_invoices = [
        remitmatch.Invoice(
            str(invoice.pk),
            invoice.reference,
            balance,
            invoice.currency,
            invoice.issue_date,  # type: ignore[arg-type]
            invoice.due_date,  # type: ignore[arg-type]
            (invoice.customer_details or {}).get("name", ""),
            scope,
        )
        for invoice, balance in invoices
    ]
    payments = [
        remitmatch.Payment(
            str(tx.pk),
            tx.amount,
            tx.bank_account.currency,
            tx.value_date,
            tx.description,
            tx.payer,
            scope,
        )
        for tx in transactions
    ]
    return remitmatch.reconcile(engine_invoices, payments, remitmatch.MatchConfig(**config))  # type: ignore[arg-type]
