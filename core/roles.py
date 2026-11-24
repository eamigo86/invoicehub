"""Helpers for the database role split between the migrator and the runtime app role."""

import re

from django.core.exceptions import ImproperlyConfigured

ROLE_NAME_RE = re.compile(r"[a-z_][a-z0-9_]*")


def validate_role_name(name: str) -> str:
    """Validate a PostgreSQL role name taken from configuration.

    Args:
        name: Candidate role name.

    Returns:
        The same name when it is a plain lowercase identifier.

    Raises:
        ImproperlyConfigured: If the name does not match the identifier pattern.
    """
    if not ROLE_NAME_RE.fullmatch(name):
        raise ImproperlyConfigured(f"Invalid database role name: {name!r}")
    return name


def check_migrator(current_user: str, migrations_owner: str | None, app_role: str) -> None:
    """Ensure the connecting role is the schema owner and not the runtime role.

    Args:
        current_user: Role the migration is running as.
        migrations_owner: Owner of the django_migrations table, if it exists.
        app_role: Name of the runtime role being granted privileges.

    Raises:
        ImproperlyConfigured: If the current role is the app role, or does not own
            django_migrations.
    """
    if current_user == app_role:
        raise ImproperlyConfigured("Migrations must not run as the app role")
    if migrations_owner != current_user:
        raise ImproperlyConfigured(
            f"Migrations must run as the owner of django_migrations, not {current_user!r}"
        )
