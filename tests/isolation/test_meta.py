"""Meta-tests that keep the isolation suite complete as models are added."""

import pytest
from django.contrib import admin
from django.db import connection

from tenancy.admin import TenantModelAdmin
from tests.isolation.registry import FACTORIES, concrete_tenant_models
from tests.test_rls_operation import EXPECTED

pytestmark = pytest.mark.isolation


def test_every_tenant_model_has_a_factory() -> None:
    """A concrete tenant model missing from the registry fails here."""
    missing = [m.__name__ for m in concrete_tenant_models() if m not in FACTORIES]
    assert missing == []
    assert concrete_tenant_models(), "no tenant model found, the suite would be vacuous"


@pytest.mark.django_db
def test_every_tenant_table_enforces_row_level_security() -> None:
    """Each tenant table has RLS enabled and forced, with the expected isolation policy."""
    for model in concrete_tenant_models():
        table = model._meta.db_table
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
                [table],
            )
            flags = cursor.fetchone()
            cursor.execute(
                "SELECT policyname, qual, with_check FROM pg_policies WHERE tablename = %s",
                [table],
            )
            policies = cursor.fetchall()
        assert flags == (True, True), table
        assert policies == [("tenant_isolation", EXPECTED, EXPECTED)], table


def test_every_tenant_model_admin_hides_without_a_tenant() -> None:
    """Each admin registered for a tenant model derives from TenantModelAdmin."""
    wrong = [
        model.__name__
        for model in concrete_tenant_models()
        if model in admin.site._registry
        and not isinstance(admin.site._registry[model], TenantModelAdmin)
    ]
    assert wrong == []
