from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def grant_app_role(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Grant the runtime role DML on current and future tables and sequences in public."""
    q = schema_editor.quote_name
    role = q(settings.DATABASE_APP_ROLE)
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        owner = q(cursor.fetchone()[0])
        for sql in (
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}",
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
            f"GRANT USAGE, SELECT ON SEQUENCES TO {role}",
        ):
            cursor.execute(sql)


class Migration(migrations.Migration):
    dependencies = []

    operations = [migrations.RunPython(grant_app_role, migrations.RunPython.noop)]
