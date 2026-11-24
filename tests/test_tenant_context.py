"""Current tenant tracking through a context variable and tenant_context."""

import uuid

import pytest
from django.db import connection

from tenancy.context import (
    TenantNotSet,
    apply_tenant,
    get_current_tenant_id,
    require_current_tenant_id,
    tenant_context,
)

pytestmark = pytest.mark.django_db

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


def test_no_tenant_outside_context() -> None:
    """Outside any tenant_context the current tenant is None and requiring one raises."""
    assert get_current_tenant_id() is None
    with pytest.raises(TenantNotSet):
        require_current_tenant_id()


def test_context_sets_and_restores_the_variable() -> None:
    """Inside tenant_context the variable holds the tenant; it is reset on exit."""
    with tenant_context(A):
        assert get_current_tenant_id() == A
        assert require_current_tenant_id() == A
    assert get_current_tenant_id() is None


def test_context_accepts_a_string_and_rejects_garbage() -> None:
    """A UUID string is accepted; anything else raises ValueError and changes nothing."""
    with tenant_context(str(A)):
        assert get_current_tenant_id() == A
    with pytest.raises(ValueError), tenant_context("not-a-uuid"):
        pass
    assert get_current_tenant_id() is None


def test_context_resets_the_variable_when_the_body_raises() -> None:
    """An exception in the body still resets the variable.

    Raises:
        RuntimeError: Deliberately, from inside the block, and caught by the test.
    """
    with pytest.raises(RuntimeError), tenant_context(A):
        raise RuntimeError("boom")
    assert get_current_tenant_id() is None


def test_nested_contexts_restore_the_outer_tenant() -> None:
    """Nested A then B returns to A on exit, in the variable and in the database setting."""
    with tenant_context(A):
        assert setting() == str(A)
        with tenant_context(B):
            assert get_current_tenant_id() == B
            assert setting() == str(B)
        assert get_current_tenant_id() == A
        assert setting() == str(A)
    assert get_current_tenant_id() is None
    assert setting() == ""


def test_context_inside_an_atomic_block_applies_immediately() -> None:
    """Entered inside the test transaction, the setting is visible right away."""
    with tenant_context(A):
        assert setting() == str(A)


def test_apply_tenant_without_tenant_sets_the_empty_string() -> None:
    """No tenant is applied as the empty string, which RLS treats as no tenant."""
    with connection.cursor() as cursor:
        apply_tenant(cursor, A)
        apply_tenant(cursor, None)
    assert setting() == ""
