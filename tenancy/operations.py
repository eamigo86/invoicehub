"""Migration operations for tenant isolation."""

from typing import Any

from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ProjectState

POLICY = "tenant_isolation"
TENANT_EXPRESSION = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
# The same expression as PostgreSQL reports it in pg_policies, for tests that read it back.
POLICY_QUAL = "(tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)"


class EnableRowLevelSecurity(Operation):
    """Enable and force Row-Level Security on a tenant model's table.

    The policy matches rows whose tenant_id equals the transaction-local app.tenant_id, for
    both reads and writes. An empty or missing setting becomes NULL and matches no row, so the
    policy fails closed. FORCE keeps the table owner bound by the policy too.
    """

    reversible = True

    def __init__(self, model_name: str) -> None:
        """Remember the model whose table is protected.

        Args:
            model_name: Name of the tenant model, in any letter case.
        """
        self.model_name = model_name

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        """Describe how to rebuild the operation in a migration file.

        Returns:
            The class name, positional arguments and keyword arguments.
        """
        return (self.__class__.__name__, [], {"model_name": self.model_name})

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        """Leave the project state untouched, since only the database changes.

        Args:
            app_label: Label of the app owning the migration, unused.
            state: Project state to mutate, unchanged.
        """

    def _table(
        self, app_label: str, schema_editor: BaseDatabaseSchemaEditor, state: ProjectState
    ) -> str:
        """Resolve the quoted table name of the model.

        Args:
            app_label: Label of the app owning the model.
            schema_editor: Editor used to quote the name.
            state: Project state in which to look the model up.

        Returns:
            The quoted table name.
        """
        model = state.apps.get_model(app_label, self.model_name)
        return schema_editor.quote_name(model._meta.db_table)

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        """Enable and force Row-Level Security and create the isolation policy.

        Args:
            app_label: Label of the app owning the model.
            schema_editor: Editor whose connection runs the statements.
            from_state: State before the operation, unused.
            to_state: State after the operation, used to find the table.
        """
        table = self._table(app_label, schema_editor, to_state)
        for sql in (
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
            f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
            f"CREATE POLICY {POLICY} ON {table} "
            f"USING ({TENANT_EXPRESSION}) WITH CHECK ({TENANT_EXPRESSION})",
        ):
            schema_editor.execute(sql)

    def database_backwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        """Drop the policy and turn Row-Level Security off again.

        Args:
            app_label: Label of the app owning the model.
            schema_editor: Editor whose connection runs the statements.
            from_state: State before the reversal, used to find the table.
            to_state: State after the reversal, unused.
        """
        table = self._table(app_label, schema_editor, from_state)
        for sql in (
            f"DROP POLICY {POLICY} ON {table}",
            f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY",
            f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY",
        ):
            schema_editor.execute(sql)

    def describe(self) -> str:
        """Summarise the operation for migration output.

        Returns:
            A one-line description.
        """
        return f"Enable row-level security on {self.model_name}"

    @property
    def migration_name_fragment(self) -> str:
        """Name fragment used when a migration is auto-named after this operation.

        Returns:
            A lower-case fragment naming the model.
        """
        return f"enable_rls_{self.model_name.lower()}"
