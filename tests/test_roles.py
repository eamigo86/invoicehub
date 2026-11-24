"""Unit tests for the role helpers and for role name validation in settings."""

import pytest
from django.core.exceptions import ImproperlyConfigured

from core.roles import check_migrator, validate_role_name
from tests.test_settings import run_settings


@pytest.mark.parametrize("name", ["invoicehub_app", "_app1", "a"])
def test_valid_role_names_pass(name: str) -> None:
    """Lowercase identifiers are accepted unchanged.

    Args:
        name: Role name under test.
    """
    assert validate_role_name(name) == name


@pytest.mark.parametrize("name", ["", "App", "1app", "a-b", 'a"; DROP ROLE x; --', "a b", "a\n"])
def test_invalid_role_names_are_rejected(name: str) -> None:
    """Anything outside the lowercase identifier pattern is refused.

    Args:
        name: Role name under test.
    """
    with pytest.raises(ImproperlyConfigured):
        validate_role_name(name)


def test_check_migrator_accepts_the_owner() -> None:
    """The owner of django_migrations, distinct from the app role, passes."""
    check_migrator("invoicehub_migrator", "invoicehub_migrator", "invoicehub_app")


def test_check_migrator_rejects_a_non_owner() -> None:
    """A role that does not own django_migrations is refused."""
    with pytest.raises(ImproperlyConfigured, match="owner"):
        check_migrator("someone_else", "invoicehub_migrator", "invoicehub_app")


def test_check_migrator_rejects_the_app_role() -> None:
    """The app role itself must never run the grants migration."""
    with pytest.raises(ImproperlyConfigured, match="app role"):
        check_migrator("invoicehub_app", "invoicehub_app", "invoicehub_app")


def test_check_migrator_rejects_missing_table() -> None:
    """A missing django_migrations table is refused."""
    with pytest.raises(ImproperlyConfigured):
        check_migrator("invoicehub_migrator", None, "invoicehub_app")


def test_settings_reject_unsafe_app_role() -> None:
    """Settings refuse a DATABASE_APP_ROLE that is not a plain identifier."""
    result = run_settings(
        {
            "SECRET_KEY": "k",
            "DATABASE_URL": "postgres://u:p@h:1/d",
            "DATABASE_APP_ROLE": 'x"; DROP ROLE y; --',
        }
    )
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
