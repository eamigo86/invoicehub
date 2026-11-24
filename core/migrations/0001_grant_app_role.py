"""Grant the runtime database role DML on current and future tables in the public schema."""

from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

from core.roles import check_migrator, validate_role_name


def grant_app_role(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Grant the app role DML on tables and sequences, now and for future objects.

    Default privileges are registered for the connecting role, so it must be the schema
    owner. The app role is denied writes to the migration history table.

    Args:
        apps: Historical app registry supplied by the migration framework, unused.
        schema_editor: Schema editor whose connection runs the grants.
    """
    q = schema_editor.quote_name
    app_role = validate_role_name(settings.DATABASE_APP_ROLE)
    role = q(app_role)
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        current = cursor.fetchone()[0]
        cursor.execute(
            "SELECT tableowner FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename = 'django_migrations'"
        )
        row = cursor.fetchone()
        check_migrator(current, row[0] if row else None, app_role)
        owner = q(current)
        for sql in (
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}",
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}",
            f"REVOKE INSERT, UPDATE, DELETE ON public.django_migrations FROM {role}",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
            f"GRANT USAGE, SELECT ON SEQUENCES TO {role}",
        ):
            cursor.execute(sql)


class Migration(migrations.Migration):
    """Grants the runtime role its privileges; reversal is a no-op."""

    dependencies = []

    operations = [migrations.RunPython(grant_app_role, migrations.RunPython.noop)]
