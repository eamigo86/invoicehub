"""PostgreSQL database wrapper that scopes every transaction to the current tenant."""

from django.db.backends.postgresql.base import DatabaseWrapper as PostgresWrapper

from tenancy.context import apply_tenant, get_current_tenant_id


class DatabaseWrapper(PostgresWrapper):
    """PostgreSQL wrapper that applies the current tenant as the first statement of a transaction.

    Django starts an outermost transaction by switching autocommit off, which for psycopg only
    flips a connection flag; the first statement then opens the transaction. Hooking the
    autocommit switch makes the tenant configuration that first statement, runs once per
    outermost transaction (savepoints never reach it), and sees the tenant before any query.
    """

    def _set_autocommit(self, autocommit: bool) -> None:
        """Switch autocommit and configure the tenant when a transaction begins.

        Args:
            autocommit: The new autocommit state; False begins a transaction.
        """
        super()._set_autocommit(autocommit)  # type: ignore[misc]
        if not autocommit:
            with self.wrap_database_errors, self.connection.cursor() as cursor:
                apply_tenant(cursor, get_current_tenant_id())
