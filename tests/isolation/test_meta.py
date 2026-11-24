"""Meta-tests that keep the isolation suite complete as models are added."""

import pytest
from django.contrib import admin
from django.db import connection, models
from django.test.utils import isolate_apps

from tenancy.admin import TenantModelAdmin
from tenancy.models import Tenant, TenantModel
from tenancy.operations import POLICY, POLICY_QUAL
from tests.isolation.registry import (
    FACTORIES,
    TENANT_FK_ALLOWLIST,
    concrete_tenant_models,
    non_tenant_models,
    unprotected_tenant_fk_models,
)

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
        assert policies == [(POLICY, POLICY_QUAL, POLICY_QUAL)], table


def test_every_tenant_model_admin_hides_without_a_tenant() -> None:
    """Each admin registered for a tenant model derives from TenantModelAdmin."""
    wrong = [
        model.__name__
        for model in concrete_tenant_models()
        if model in admin.site._registry
        and not isinstance(admin.site._registry[model], TenantModelAdmin)
    ]
    assert wrong == []


def test_every_business_model_is_a_tenant_model() -> None:
    """Business apps contain only tenant models, so no business data escapes RLS."""
    assert non_tenant_models() == []


def test_the_business_model_check_flags_plain_and_implicit_through_models() -> None:
    """The check reports a plain model and the auto-created table of a many-to-many."""
    with isolate_apps("billing") as iso:

        class Plain(models.Model):
            """Model that is not tenant owned."""

            class Meta:
                """Model options."""

                app_label = "billing"

        class Tagged(TenantModel):
            """Tenant model with an implicit many-to-many through table."""

            plains = models.ManyToManyField(Plain)

            class Meta:
                """Model options."""

                app_label = "billing"

        assert iso is not None
        assert Tagged is not None
        assert non_tenant_models(iso, ("billing",)) == ["billing.Plain", "billing.Tagged_plains"]


def test_every_model_with_a_tenant_foreign_key_is_a_tenant_model_or_allowlisted() -> None:
    """A plain model pointing at a tenant must justify why it has no Row-Level Security."""
    assert unprotected_tenant_fk_models() == []
    assert all(reason for reason in TENANT_FK_ALLOWLIST.values())


def test_the_tenant_foreign_key_check_flags_only_unlisted_plain_models() -> None:
    """A plain model with a tenant key is reported until it is allowlisted."""
    with isolate_apps("billing") as iso:

        class Plain(models.Model):
            """Plain model that points at a tenant."""

            tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="+")

            class Meta:
                """Model options."""

                app_label = "billing"

        class Owned(TenantModel):
            """Tenant model, safe by construction."""

            class Meta:
                """Model options."""

                app_label = "billing"

        class Unrelated(models.Model):
            """Plain model with no tenant key."""

            class Meta:
                """Model options."""

                app_label = "billing"

        assert iso is not None
        assert Owned is not None
        assert Unrelated is not None
        assert unprotected_tenant_fk_models(iso, ("billing",), {}) == ["billing.Plain"]
        assert unprotected_tenant_fk_models(iso, ("billing",), {Plain: "test reason"}) == []
