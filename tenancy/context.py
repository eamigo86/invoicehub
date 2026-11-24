"""Current tenant tracking and its propagation to the database.

The current tenant lives in a context variable. It reaches PostgreSQL as the transaction-local
setting app.tenant_id, which Row-Level Security policies read. The setting is applied by the
database backend when a transaction begins, and by tenant_context when it runs inside an
already open transaction.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import UUID

from django.db import connection, transaction

__all__ = [
    "TenantNotSet",
    "apply_tenant",
    "current_tenant_id",
    "get_current_tenant_id",
    "parse_tenant_id",
    "require_current_tenant_id",
    "tenant_context",
]


class TenantNotSet(Exception):
    """Raised when tenant-scoped code runs without an active tenant."""


current_tenant_id: ContextVar[UUID | None] = ContextVar("current_tenant_id", default=None)


def get_current_tenant_id() -> UUID | None:
    """Return the active tenant.

    Returns:
        The id of the current tenant, or None when no tenant is active.
    """
    return current_tenant_id.get()


def require_current_tenant_id() -> UUID:
    """Return the active tenant or fail.

    Returns:
        The id of the current tenant.

    Raises:
        TenantNotSet: When no tenant is active.
    """
    tenant_id = current_tenant_id.get()
    if tenant_id is None:
        raise TenantNotSet("no tenant is active in the current context")
    return tenant_id


def parse_tenant_id(value: object) -> UUID:
    """Convert a UUID or its string form into a UUID.

    Args:
        value: A UUID instance or a string holding a UUID.

    Returns:
        The parsed UUID.

    Raises:
        ValueError: When the value is not a valid UUID.
    """
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except ValueError as exc:
        raise ValueError(f"invalid tenant id: {value!r}") from exc


def apply_tenant(cursor: Any, tenant_id: UUID | None) -> None:
    """Apply a tenant to the current database transaction.

    The setting is transaction-local, so it vanishes on commit or rollback. No tenant is
    applied as the empty string, which Row-Level Security policies treat as no tenant.

    Args:
        cursor: Database cursor inside the transaction to configure.
        tenant_id: Tenant to apply, or None for no tenant.
    """
    cursor.execute(
        "SELECT set_config('app.tenant_id', %s, true)",
        ["" if tenant_id is None else str(tenant_id)],
    )


@contextmanager
def tenant_context(tenant_id: UUID | str) -> Iterator[None]:
    """Run a block as a tenant inside its own transaction.

    Celery tasks use this with their explicit tenant_id argument. Inside an already open
    transaction the setting is applied immediately and the previous tenant is restored on
    exit. An invalid tenant id raises ValueError before anything changes.

    Args:
        tenant_id: Tenant to activate, as a UUID or its string form.

    Yields:
        Nothing; the tenant is active while the block runs.
    """
    value = parse_tenant_id(tenant_id)
    previous = current_tenant_id.get()
    nested = connection.in_atomic_block
    token = current_tenant_id.set(value)
    try:
        with transaction.atomic():
            if nested:
                with connection.cursor() as cursor:
                    apply_tenant(cursor, value)
            yield
    finally:
        current_tenant_id.reset(token)
        if connection.in_atomic_block and not connection.needs_rollback:
            with connection.cursor() as cursor:
                apply_tenant(cursor, previous)
