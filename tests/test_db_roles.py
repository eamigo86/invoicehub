"""Database role separation: migrator owns the schema, the app role only reads and writes."""

import pytest
from django.conf import settings
from django.contrib.auth.models import Group
from django.db import ProgrammingError, connection, transaction

pytestmark = pytest.mark.django_db

ROLE_FLAGS = "SELECT rolsuper, rolbypassrls, rolcreatedb FROM pg_roles WHERE rolname = %s"


def role_flags(name: str) -> tuple[bool, bool, bool]:
    """Fetch superuser, bypass-RLS and create-database flags for a role.

    Args:
        name: Role name to look up.

    Returns:
        Tuple of rolsuper, rolbypassrls and rolcreatedb.
    """
    with connection.cursor() as cursor:
        cursor.execute(ROLE_FLAGS, [name])
        row = cursor.fetchone()
    assert row is not None, f"role {name} does not exist"
    return (bool(row[0]), bool(row[1]), bool(row[2]))


def test_runtime_role_is_unprivileged() -> None:
    """The app role exists, is not a superuser and cannot bypass RLS or create databases."""
    assert role_flags(settings.DATABASE_APP_ROLE) == (False, False, False)


def test_connecting_role_cannot_bypass_rls() -> None:
    """The migrator role we connect as is not a superuser and has no BYPASSRLS."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        row = cursor.fetchone()
    assert row is not None
    superuser, bypass, _ = role_flags(row[0])
    assert (superuser, bypass) == (False, False)


def test_app_role_can_read_and_write_tables() -> None:
    """Under SET LOCAL ROLE the app role can insert and select Django table rows."""
    with connection.cursor() as cursor:
        cursor.execute(f"SET LOCAL ROLE {connection.ops.quote_name(settings.DATABASE_APP_ROLE)}")
    Group.objects.create(name="roles-test")
    assert Group.objects.filter(name="roles-test").exists()


def test_app_role_cannot_create_tables() -> None:
    """Under SET LOCAL ROLE the app role is denied CREATE TABLE in the public schema."""
    with connection.cursor() as cursor:
        cursor.execute(f"SET LOCAL ROLE {connection.ops.quote_name(settings.DATABASE_APP_ROLE)}")
        with pytest.raises(ProgrammingError), transaction.atomic():
            cursor.execute("CREATE TABLE public.should_fail (id int)")
