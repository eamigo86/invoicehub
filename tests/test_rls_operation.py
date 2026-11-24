"""EnableRowLevelSecurity migration operation."""

import pytest
from django.db import connection, migrations, models
from django.db.migrations.state import ProjectState

from tenancy.operations import POLICY, POLICY_QUAL, EnableRowLevelSecurity

pytestmark = pytest.mark.django_db


def build_table() -> ProjectState:
    """Create a throwaway table with a tenant_id column through a real CreateModel.

    Returns:
        The project state that contains the new model.
    """
    create = migrations.CreateModel(
        "Probe",
        [("id", models.AutoField(primary_key=True)), ("tenant_id", models.UUIDField())],
    )
    before = ProjectState()
    after = before.clone()
    create.state_forwards("tenancy", after)
    with connection.schema_editor() as editor:
        create.database_forwards("tenancy", editor, before, after)
    return after


def table_flags() -> tuple[bool, bool]:
    """Read the row-level security flags of the probe table.

    Returns:
        Tuple of relrowsecurity and relforcerowsecurity.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = 'tenancy_probe'"
        )
        row = cursor.fetchone()
    assert row is not None
    return (row[0], row[1])


def policies() -> list[tuple[str, str, str]]:
    """List the policies of the probe table.

    Returns:
        Tuples of policy name, USING expression and WITH CHECK expression.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT policyname, qual, with_check FROM pg_policies WHERE tablename = 'tenancy_probe'"
        )
        return [(r[0], r[1], r[2]) for r in cursor.fetchall()]


def test_forwards_enables_forces_and_creates_the_policy() -> None:
    """Forwards turns on RLS, forces it for the owner and adds the isolation policy."""
    state = build_table()
    assert table_flags() == (False, False)
    with connection.schema_editor() as editor:
        EnableRowLevelSecurity("Probe").database_forwards("tenancy", editor, state, state)
    assert table_flags() == (True, True)
    assert policies() == [(POLICY, POLICY_QUAL, POLICY_QUAL)]


def test_backwards_removes_everything() -> None:
    """Backwards drops the policy and turns off FORCE and ENABLE."""
    state = build_table()
    operation = EnableRowLevelSecurity("Probe")
    with connection.schema_editor() as editor:
        operation.database_forwards("tenancy", editor, state, state)
        operation.database_backwards("tenancy", editor, state, state)
    assert table_flags() == (False, False)
    assert policies() == []


def test_operation_metadata() -> None:
    """The operation deconstructs, describes itself and names its migration."""
    operation = EnableRowLevelSecurity("Customer")
    assert operation.deconstruct() == (
        "EnableRowLevelSecurity",
        [],
        {"model_name": "Customer"},
    )
    assert operation.describe() == "Enable row-level security on Customer"
    assert operation.migration_name_fragment == "enable_rls_customer"
    assert operation.reversible is True
    state = ProjectState()
    operation.state_forwards("tenancy", state)
    assert state.models == {}
