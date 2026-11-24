"""Meta-tests that keep the isolation suite complete as models are added."""

import pytest
from django.contrib import admin
from django.db import connection, models
from django.http import HttpRequest, HttpResponse
from django.test.utils import isolate_apps
from django.urls import URLPattern, URLResolver, include, path

from tenancy.admin import TenantModelAdmin
from tenancy.models import Tenant, TenantModel
from tenancy.operations import POLICY, POLICY_QUAL
from tests.isolation.registry import (
    FACTORIES,
    TENANT_FK_ALLOWLIST,
    URL_ISOLATION_CASES,
    concrete_tenant_models,
    non_tenant_models,
    project_url_names,
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


def own_view(request: HttpRequest) -> HttpResponse:
    """Serve a view that belongs to the project.

    Args:
        request: Current request.

    Returns:
        An empty response.
    """
    return HttpResponse()


def allauth_view(request: HttpRequest) -> HttpResponse:
    """Serve a view that claims to live in the allauth package.

    Args:
        request: Current request.

    Returns:
        An empty response.
    """
    return HttpResponse()


allauth_view.__module__ = "allauth.fake"


def test_every_project_url_name_has_isolation_cases() -> None:
    """A project URL added without listing it in the registry fails here."""
    import config.urls

    names = project_url_names(config.urls.urlpatterns)
    assert names, "no project URL found, the check would be vacuous"
    assert sorted(set(names) - URL_ISOLATION_CASES) == []


def test_the_url_check_skips_admin_allauth_and_unnamed_patterns() -> None:
    """Only named, project owned, non-admin URLs are reported, with their namespace."""
    admin_like = include(([path("", own_view, name="index")], "admin"))
    shop = include(([path("", own_view, name="list"), path("x/", own_view)], "shop"))
    patterns: list[URLPattern | URLResolver] = [
        path("a/", own_view, name="a"),
        path("unnamed/", own_view),
        path("allauth/", allauth_view, name="allauth_page"),
        path("admin/", admin_like),
        path("shop/", shop),
    ]
    assert project_url_names(patterns) == ["a", "shop:list"]
    patterns.append(path("new/", own_view, name="new"))
    assert project_url_names(patterns) == ["a", "shop:list", "new"]
    assert project_url_names(patterns, skip_namespaces=(), skip_modules=()) == [
        "a",
        "allauth_page",
        "admin:index",
        "shop:list",
        "new",
    ]
