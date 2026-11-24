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


def set_app_role() -> None:
    """Switch the current transaction to the runtime role."""
    with connection.cursor() as cursor:
        cursor.execute(f"SET LOCAL ROLE {connection.ops.quote_name(settings.DATABASE_APP_ROLE)}")


def scalar(sql: str, params: list[str]) -> object:
    """Run a query and return its first column of the first row.

    Args:
        sql: Query text.
        params: Query parameters.

    Returns:
        The scalar value.
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
    assert row is not None
    return row[0]


def test_app_role_cannot_tamper_with_migration_history() -> None:
    """The app role may read django_migrations but not write to it."""
    app = settings.DATABASE_APP_ROLE
    query = "SELECT has_table_privilege(%s, 'django_migrations', %s)"
    assert scalar(query, [app, "SELECT"]) is True
    for privilege in ("INSERT", "UPDATE", "DELETE"):
        assert scalar(query, [app, privilege]) is False, privilege


def test_default_privileges_cover_future_tables() -> None:
    """A table created later by the migrator is usable by the app role at once."""
    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE public.future_probe (id serial PRIMARY KEY, label text)")
    set_app_role()
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO public.future_probe (label) VALUES ('x')")
        cursor.execute("SELECT label FROM public.future_probe")
        assert cursor.fetchone() == ("x",)


def test_app_role_has_sequence_privileges() -> None:
    """The app role can use and read sequences created by the migrator."""
    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE public.seq_probe (id serial PRIMARY KEY)")
    app = settings.DATABASE_APP_ROLE
    query = "SELECT has_sequence_privilege(%s, 'public.seq_probe_id_seq', %s)"
    assert scalar(query, [app, "USAGE"]) is True
    assert scalar(query, [app, "SELECT"]) is True


def test_app_role_has_no_memberships() -> None:
    """The app role belongs to no roles, so it inherits no extra privileges."""
    sql = (
        "SELECT count(*) FROM pg_auth_members m "
        "JOIN pg_roles r ON r.oid = m.member WHERE r.rolname = %s"
    )
    assert scalar(sql, [settings.DATABASE_APP_ROLE]) == 0


def test_app_role_owns_no_public_tables() -> None:
    """Table ownership stays with the migrator, never the runtime role."""
    sql = "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tableowner = %s"
    assert scalar(sql, [settings.DATABASE_APP_ROLE]) == 0
