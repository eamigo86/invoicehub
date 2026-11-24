"""Tenant A must never read or change tenant B's rows, through the ORM or raw SQL."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import pytest
from django.conf import settings
from django.db import ProgrammingError, connection, transaction
from psycopg import errors

from tenancy.context import TenantMismatch, apply_tenant, tenant_context
from tenancy.models import Tenant, TenantModel
from tests.isolation.registry import FACTORIES, concrete_tenant_models

pytestmark = [pytest.mark.isolation, pytest.mark.django_db]


@dataclass
class World:
    """Two tenants that each own one row of the model under test.

    Attributes:
        model: Tenant model under test.
        a: Tenant A, the active one in the tests.
        b: Tenant B, the foreign one.
        a_row: Row owned by A.
        b_row: Row owned by B.
    """

    model: type[TenantModel]
    a: Tenant
    b: Tenant
    a_row: TenantModel
    b_row: TenantModel


@pytest.fixture(params=concrete_tenant_models(), ids=lambda m: m.__name__)
def world(request: pytest.FixtureRequest) -> World:
    """Build two tenants with one row each for a registered model.

    Args:
        request: Fixture request carrying the model parameter.

    Returns:
        The populated world.
    """
    model: type[TenantModel] = request.param
    a, b = Tenant.objects.create(name="A"), Tenant.objects.create(name="B")
    with tenant_context(a.pk):
        a_row = FACTORIES[model]()
    with tenant_context(b.pk):
        b_row = FACTORIES[model]()
    return World(model, a, b, a_row, b_row)


@contextmanager
def as_role(role: str | None) -> Iterator[None]:
    """Run a block under a database role, inside the current transaction.

    Args:
        role: Role to switch to, or None to stay as the connecting role.

    Yields:
        Nothing; the original role is restored afterwards.
    """
    if role is None:
        yield
        return
    with connection.cursor() as cursor:
        cursor.execute(f"SET LOCAL ROLE {connection.ops.quote_name(role)}")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")


ROLES = pytest.mark.parametrize(
    "role", [None, settings.DATABASE_APP_ROLE], ids=["migrator", "app_role"]
)


def names(world: World) -> tuple[str, str, str]:
    """Quote the table and key column of the model under test.

    Args:
        world: World whose model is inspected.

    Returns:
        The quoted table name, primary key column and tenant column.
    """
    q = connection.ops.quote_name
    meta = world.model._meta
    assert meta.pk is not None
    return q(meta.db_table), q(meta.pk.column or ""), q("tenant_id")


def test_manager_sees_only_the_current_tenant(world: World) -> None:
    """The default manager returns A's row and nothing else.

    Args:
        world: Two tenants with one row each.
    """
    with tenant_context(world.a.pk):
        assert list(world.model.objects.values_list("pk", flat=True)) == [world.a_row.pk]


def test_base_manager_is_stopped_by_rls_not_by_the_manager(world: World) -> None:
    """Bypassing the scoped manager still finds nothing of B, because RLS filters the rows.

    Args:
        world: Two tenants with one row each.
    """
    with tenant_context(world.a.pk):
        assert not world.model._base_manager.filter(pk=world.b_row.pk).exists()
        assert world.model._base_manager.count() == 1


def test_manager_is_a_barrier_even_if_the_database_tenant_were_wrong(world: World) -> None:
    """With the database tenant forced to B, the manager still hides B's row from A.

    Args:
        world: Two tenants with one row each.
    """
    with tenant_context(world.a.pk), connection.cursor() as cursor:
        apply_tenant(cursor, world.b.pk)
        assert world.model._base_manager.filter(pk=world.b_row.pk).exists()
        assert world.model.objects.count() == 0


def test_queryset_update_and_delete_of_foreign_rows_affect_nothing(world: World) -> None:
    """Update and delete aimed at B's primary key change zero rows.

    Args:
        world: Two tenants with one row each.
    """
    with tenant_context(world.a.pk):
        for manager in (world.model.objects, world.model._base_manager):
            assert manager.filter(pk=world.b_row.pk).update(tenant_id=world.a.pk) == 0
            assert manager.filter(pk=world.b_row.pk).delete() == (0, {})
    with tenant_context(world.b.pk):
        assert world.model.objects.get(pk=world.b_row.pk).tenant_id == world.b.pk


def test_saving_a_foreign_instance_is_refused(world: World) -> None:
    """The application barrier rejects saving B's instance while A is active.

    Args:
        world: Two tenants with one row each.
    """
    with tenant_context(world.a.pk), pytest.raises(TenantMismatch):
        world.b_row.save()


@ROLES
def test_raw_select_update_delete_of_foreign_rows_affect_nothing(
    world: World, role: str | None
) -> None:
    """Raw SQL by B's key matches zero rows for reads and writes, as migrator and app role.

    Args:
        world: Two tenants with one row each.
        role: Database role to run as, None for the migrator.
    """
    table, pk, tenant = names(world)
    with tenant_context(world.a.pk), as_role(role), connection.cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM {table} WHERE {pk} = %s", [world.b_row.pk])
        assert cursor.fetchone() == (0,)
        cursor.execute(
            f"UPDATE {table} SET {tenant} = %s WHERE {pk} = %s", [world.a.pk, world.b_row.pk]
        )
        assert cursor.rowcount == 0
        cursor.execute(f"DELETE FROM {table} WHERE {pk} = %s", [world.b_row.pk])
        assert cursor.rowcount == 0
        cursor.execute(f"SELECT count(*) FROM {table}")
        assert cursor.fetchone() == (1,)


@ROLES
def test_raw_insert_for_another_tenant_is_rejected(world: World, role: str | None) -> None:
    """WITH CHECK rejects a row carrying B's tenant_id, while the same row for A is accepted.

    The row is cloned from A's own row, so the model needs no per-model insert statement.

    Args:
        world: Two tenants with one row each.
        role: Database role to run as, None for the migrator.
    """
    table, pk, tenant = names(world)
    q = connection.ops.quote_name
    others = ", ".join(
        q(f.column or "")
        for f in world.model._meta.concrete_fields
        if not f.primary_key and f.column != "tenant_id"
    )
    clone = (
        f"INSERT INTO {table} ({tenant}, {others}) SELECT %s, {others} FROM {table} WHERE {pk} = %s"
    )
    with tenant_context(world.a.pk), as_role(role), connection.cursor() as cursor:
        with pytest.raises(ProgrammingError) as caught, transaction.atomic():
            cursor.execute(clone, [world.b.pk, world.a_row.pk])
        assert isinstance(caught.value.__cause__, errors.InsufficientPrivilege)
        cursor.execute(clone, [world.a.pk, world.a_row.pk])
        cursor.execute(f"SELECT count(*) FROM {table}")
        assert cursor.fetchone() == (2,)


@ROLES
def test_raw_select_without_a_tenant_returns_nothing(world: World, role: str | None) -> None:
    """With no active tenant the policy matches no row at all.

    Args:
        world: Two tenants with one row each.
        role: Database role to run as, None for the migrator.
    """
    table, _, _ = names(world)
    with transaction.atomic(), as_role(role), connection.cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM {table}")
        assert cursor.fetchone() == (0,)
