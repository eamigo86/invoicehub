"""The database backend applies the current tenant when an outermost transaction begins."""

import uuid

import pytest
from django.conf import settings
from django.db import connection, transaction

from tenancy.context import current_tenant_id

A = uuid.uuid4()
B = uuid.uuid4()


def setting() -> str | None:
    """Read the tenant setting of the current database session.

    Returns:
        The value of app.tenant_id, or None when it was never set on this connection.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.tenant_id', true)")
        row = cursor.fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def test_settings_use_the_tenant_backend_with_atomic_requests() -> None:
    """The default database uses the tenancy backend and wraps every request in a transaction."""
    assert settings.DATABASES["default"]["ENGINE"] == "tenancy.db_backend"
    assert settings.DATABASES["default"]["ATOMIC_REQUESTS"] is True


@pytest.mark.django_db(transaction=True)
def test_outermost_transaction_applies_the_current_tenant() -> None:
    """The first statement of a new transaction configures the tenant held by the variable."""
    token = current_tenant_id.set(A)
    try:
        with transaction.atomic():
            assert setting() == str(A)
    finally:
        current_tenant_id.reset(token)


@pytest.mark.django_db(transaction=True)
def test_setting_does_not_survive_the_transaction() -> None:
    """After commit the setting is empty again, so a pooled connection never carries a tenant."""
    token = current_tenant_id.set(A)
    try:
        with transaction.atomic():
            assert setting() == str(A)
    finally:
        current_tenant_id.reset(token)
    assert setting() == ""


@pytest.mark.django_db(transaction=True)
def test_no_tenant_fails_closed() -> None:
    """Without a tenant the setting is empty, or NULL on a never-used connection.

    Both read as no tenant to the Row-Level Security policy, which maps the empty string to
    NULL and therefore matches no row.
    """
    with transaction.atomic():
        assert setting() in ("", None)
        assert not setting()


@pytest.mark.django_db(transaction=True)
def test_nested_atomic_does_not_reapply() -> None:
    """A savepoint is not an outermost transaction, so it keeps the tenant of the outer one."""
    token = current_tenant_id.set(A)
    try:
        with transaction.atomic():
            other = current_tenant_id.set(B)
            try:
                with transaction.atomic():
                    assert setting() == str(A)
            finally:
                current_tenant_id.reset(other)
    finally:
        current_tenant_id.reset(token)


@pytest.mark.django_db(transaction=True)
def test_each_transaction_reads_the_variable_again() -> None:
    """Two consecutive transactions get the tenant current at their own start."""
    token = current_tenant_id.set(A)
    try:
        with transaction.atomic():
            assert setting() == str(A)
    finally:
        current_tenant_id.reset(token)
    with transaction.atomic():
        assert setting() == ""
