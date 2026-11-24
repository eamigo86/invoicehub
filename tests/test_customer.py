"""Customer, the first tenant model: application barrier and Row-Level Security."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from django.conf import settings
from django.db import ProgrammingError, connection, transaction
from psycopg import errors

from billing.models import Customer
from tenancy.context import TenantMismatch, TenantNotSet, tenant_context
from tenancy.models import Tenant

pytestmark = pytest.mark.django_db

COUNT = "SELECT count(*) FROM billing_customer"
INSERT = "INSERT INTO billing_customer (tenant_id, name, tax_id, email) VALUES (%s, 'X', '', '')"


@pytest.fixture
def tenants() -> tuple[Tenant, Tenant]:
    """Create two tenants.

    Returns:
        Tenants A and B.
    """
    return Tenant.objects.create(name="A"), Tenant.objects.create(name="B")


@pytest.fixture
def seeded(tenants: tuple[Tenant, Tenant]) -> tuple[Tenant, Tenant]:
    """Create two customers for tenant A and one for tenant B.

    Args:
        tenants: Tenants A and B.

    Returns:
        Tenants A and B.
    """
    a, b = tenants
    with tenant_context(a.pk):
        Customer.objects.create(name="a1")
        Customer.objects.create(name="a2")
    with tenant_context(b.pk):
        Customer.objects.create(name="b1")
    return a, b


@contextmanager
def app_role() -> Iterator[None]:
    """Run the block as the runtime database role inside the current transaction.

    Yields:
        Nothing; the role is reverted when the transaction ends.
    """
    with connection.cursor() as cursor:
        cursor.execute(f"SET LOCAL ROLE {connection.ops.quote_name(settings.DATABASE_APP_ROLE)}")
        yield


def count_rows() -> int:
    """Count the customer rows visible to raw SQL.

    Returns:
        The number of rows the current role and tenant can see.
    """
    with connection.cursor() as cursor:
        cursor.execute(COUNT)
        row = cursor.fetchone()
    assert row is not None
    return int(row[0])


def insert_row(tenant_id: uuid.UUID) -> None:
    """Insert a customer row with raw SQL.

    Args:
        tenant_id: Value written to the tenant_id column.
    """
    with connection.cursor() as cursor:
        cursor.execute(INSERT, [tenant_id])


def test_str_is_the_name() -> None:
    """A customer prints as its name."""
    assert str(Customer(name="Acme")) == "Acme"


def test_manager_requires_a_tenant(seeded: tuple[Tenant, Tenant]) -> None:
    """Without an active tenant the manager raises TenantNotSet.

    Args:
        seeded: Tenants with customers.
    """
    with pytest.raises(TenantNotSet):
        Customer.objects.count()


def test_manager_returns_only_the_current_tenants_rows(seeded: tuple[Tenant, Tenant]) -> None:
    """Each tenant sees only its own customers through the ORM.

    Args:
        seeded: Tenants with customers.
    """
    a, b = seeded
    with tenant_context(a.pk):
        assert sorted(Customer.objects.values_list("name", flat=True)) == ["a1", "a2"]
    with tenant_context(b.pk):
        assert list(Customer.objects.values_list("name", flat=True)) == ["b1"]


def test_save_assigns_the_current_tenant(tenants: tuple[Tenant, Tenant]) -> None:
    """A new customer is stored under the active tenant.

    Args:
        tenants: Tenants A and B.
    """
    a, _ = tenants
    with tenant_context(a.pk):
        customer = Customer.objects.create(name="new")
        assert customer.tenant_id == a.pk
        assert Customer.objects.get(pk=customer.pk).tenant_id == a.pk


def test_save_of_another_tenants_instance_raises(seeded: tuple[Tenant, Tenant]) -> None:
    """An instance loaded under B cannot be saved while A is active.

    Args:
        seeded: Tenants with customers.
    """
    a, b = seeded
    with tenant_context(b.pk):
        foreign = Customer.objects.get(name="b1")
    with tenant_context(a.pk), pytest.raises(TenantMismatch):
        foreign.save()


@pytest.mark.parametrize("role", [False, True], ids=["migrator", "app_role"])
def test_raw_select_sees_only_the_current_tenant(seeded: tuple[Tenant, Tenant], role: bool) -> None:
    """Raw SQL is filtered by the policy, for the migrator and for the runtime role.

    Args:
        seeded: Tenants with customers.
        role: Whether to switch to the runtime role first.
    """
    a, b = seeded
    with tenant_context(a.pk):
        if role:
            with app_role():
                assert count_rows() == 2
        else:
            assert count_rows() == 2
    with tenant_context(b.pk):
        assert count_rows() == 1


@pytest.mark.parametrize("role", [False, True], ids=["migrator", "app_role"])
def test_raw_select_without_a_tenant_sees_nothing(
    seeded: tuple[Tenant, Tenant], role: bool
) -> None:
    """With no tenant the policy matches no row.

    Args:
        seeded: Tenants with customers.
        role: Whether to switch to the runtime role first.
    """
    with transaction.atomic():
        if role:
            with app_role():
                assert count_rows() == 0
        else:
            assert count_rows() == 0


@pytest.mark.parametrize("role", [False, True], ids=["migrator", "app_role"])
def test_raw_insert_for_another_tenant_is_rejected(
    seeded: tuple[Tenant, Tenant], role: bool
) -> None:
    """WITH CHECK refuses a row whose tenant_id is not the active tenant.

    Args:
        seeded: Tenants with customers.
        role: Whether to switch to the runtime role first.
    """
    a, b = seeded
    with tenant_context(a.pk):
        with pytest.raises(ProgrammingError) as caught, transaction.atomic():
            if role:
                with app_role():
                    insert_row(b.pk)
            else:
                insert_row(b.pk)
        assert isinstance(caught.value.__cause__, errors.InsufficientPrivilege)
        insert_row(a.pk)
        assert count_rows() == 3
