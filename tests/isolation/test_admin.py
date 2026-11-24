"""Every admin of a tenant model only shows and changes the active tenant's rows.

Cases run for each admin registered for a TenantModel, using the factory registry to create
rows. Real transactions are required: the tenant reaches PostgreSQL when a request's outermost
transaction begins.
"""

import uuid
from typing import Any

import pytest
from django.contrib import admin
from django.contrib.auth.models import Permission
from django.db import models
from django.test import Client
from django.urls import reverse

from accounts.models import User
from tenancy.context import tenant_context
from tenancy.models import Tenant, TenantModel
from tests.isolation.registry import FACTORIES

pytestmark = [pytest.mark.isolation, pytest.mark.django_db(transaction=True)]


def tenant_admin_models() -> list[type[TenantModel]]:
    """List the tenant models that have an admin registered.

    Returns:
        The model classes, ordered by name.
    """
    found = [m for m in admin.site._registry if issubclass(m, TenantModel) and not m._meta.abstract]
    return sorted(found, key=lambda m: m.__name__)


def hostile_data(model: type[TenantModel]) -> dict[str, str]:
    """Build form data that would rename every text field of a row.

    Args:
        model: Model whose admin form is posted.

    Returns:
        Field values for the text and email fields of the model.
    """
    data: dict[str, str] = {}
    for field in model._meta.fields:
        if isinstance(field, models.EmailField):
            data[field.name] = "hijacked@example.com"
        elif isinstance(field, models.CharField) and field.editable and not field.primary_key:
            data[field.name] = "hijacked"
    return data


def listed_pks(response: Any) -> list[object]:
    """Read the primary keys shown on an admin changelist.

    Args:
        response: Rendered changelist response.

    Returns:
        The primary keys of the listed rows.
    """
    context = getattr(response, "context_data", None)
    assert context is not None
    return [row.pk for row in context["cl"].result_list]


class Scenario:
    """Two tenants owning one row each of a model, and a client logged in as a superuser.

    Attributes:
        model: Tenant model under test.
        a: Tenant A.
        b: Tenant B.
        a_row: Row owned by A.
        b_row: Row owned by B.
        client: Logged in test client with no session tenant yet.
    """

    def __init__(self, model: type[TenantModel], client: Client) -> None:
        """Create the tenants and rows, and log in.

        Args:
            model: Tenant model under test.
            client: Test client to log in.
        """
        self.model = model
        self.a = Tenant.objects.create(name="A")
        self.b = Tenant.objects.create(name="B")
        with tenant_context(self.a.pk):
            self.a_row = FACTORIES[model]()
        with tenant_context(self.b.pk):
            self.b_row = FACTORIES[model]()
        self.client = client
        client.force_login(User.objects.create_superuser("root", "root@example.com", "pw"))

    def url(self, kind: str, row: TenantModel | None = None) -> str:
        """Build an admin URL of the model.

        Args:
            kind: Admin URL name suffix such as changelist, change or delete.
            row: Row the URL refers to, if it takes one.

        Returns:
            The URL path.
        """
        opts = self.model._meta
        name = f"admin:{opts.app_label}_{opts.model_name}_{kind}"
        return reverse(name, args=[row.pk] if row else [])

    def activate(self, tenant_id: uuid.UUID) -> None:
        """Put a tenant in the session.

        Args:
            tenant_id: Tenant to make active.
        """
        session = self.client.session
        session["tenant_id"] = str(tenant_id)
        session.save()

    def snapshot(self, tenant: Tenant, row: TenantModel) -> dict[str, object] | None:
        """Read a row's fields directly under its own tenant.

        Args:
            tenant: Tenant that owns the row.
            row: Row to read.

        Returns:
            The row's field values, or None when it no longer exists.
        """
        with tenant_context(tenant.pk):
            found = self.model.objects.filter(pk=row.pk).values().first()
        return found


@pytest.fixture(params=tenant_admin_models(), ids=lambda m: m.__name__)
def scenario(request: pytest.FixtureRequest, client: Client) -> Scenario:
    """Create the admin scenario for a registered tenant model.

    Args:
        request: Fixture request carrying the model parameter.
        client: Django test client fixture.

    Returns:
        The prepared scenario, with no tenant active yet.
    """
    return Scenario(request.param, client)


def test_admin_models_are_covered() -> None:
    """At least one tenant admin exists, so the parametrized cases are not vacuous."""
    assert tenant_admin_models()


def test_changelist_shows_only_the_active_tenants_rows(scenario: Scenario) -> None:
    """A's row is listed and B's is not.

    Args:
        scenario: Admin scenario for one tenant model.
    """
    scenario.activate(scenario.a.pk)
    response = scenario.client.get(scenario.url("changelist"))
    assert listed_pks(response) == [scenario.a_row.pk]
    body = response.content.decode()
    assert str(scenario.a_row) in body
    assert str(scenario.b_row) not in body


@pytest.mark.parametrize("kind", ["change", "delete", "history"])
def test_foreign_object_views_do_not_render(scenario: Scenario, kind: str) -> None:
    """B's change, delete and history pages are not reachable and never show B's data.

    Args:
        scenario: Admin scenario for one tenant model.
        kind: Admin URL name suffix.
    """
    scenario.activate(scenario.a.pk)
    url = scenario.url(kind, scenario.b_row)
    assert scenario.client.get(url).status_code == 302
    assert str(scenario.b_row) not in scenario.client.get(url, follow=True).content.decode()


def test_posts_to_foreign_rows_change_nothing_but_own_rows_still_work(scenario: Scenario) -> None:
    """Change and delete POSTs aimed at B leave it untouched, while the same POSTs work for A.

    Args:
        scenario: Admin scenario for one tenant model.
    """
    before = scenario.snapshot(scenario.b, scenario.b_row)
    scenario.activate(scenario.a.pk)
    data = hostile_data(scenario.model)
    scenario.client.post(scenario.url("change", scenario.b_row), data)
    scenario.client.post(scenario.url("delete", scenario.b_row), {"post": "yes"})
    assert scenario.snapshot(scenario.b, scenario.b_row) == before
    scenario.client.post(scenario.url("change", scenario.a_row), data)
    changed = scenario.snapshot(scenario.a, scenario.a_row)
    assert changed is not None
    assert "hijacked" in {str(v) for v in changed.values()}
    scenario.client.post(scenario.url("delete", scenario.a_row), {"post": "yes"})
    assert scenario.snapshot(scenario.a, scenario.a_row) is None


def test_admin_is_hidden_without_a_session_tenant(scenario: Scenario) -> None:
    """With no tenant in the session the admin of each tenant model is not accessible.

    Args:
        scenario: Admin scenario for one tenant model.
    """
    assert scenario.client.get(scenario.url("changelist")).status_code == 403


def test_activate_action_selects_the_tenant_for_superusers(client: Client) -> None:
    """A superuser activates a tenant by POST and then sees only that tenant's rows.

    Args:
        client: Django test client fixture.
    """
    scenario = Scenario(tenant_admin_models()[0], client)
    client.post(f"/admin/tenancy/tenant/{scenario.a.pk}/activate/")
    response = client.get(scenario.url("changelist"))
    assert listed_pks(response) == [scenario.a_row.pk]


def test_activate_action_is_refused_for_non_superusers(client: Client) -> None:
    """A staff user without superuser rights cannot select a tenant.

    Args:
        client: Django test client fixture.
    """
    scenario = Scenario(tenant_admin_models()[0], client)
    staff = User.objects.create_user("staff", "staff@example.com", "pw", is_staff=True)
    staff.user_permissions.add(Permission.objects.get(codename="view_tenant"))
    client.force_login(staff)
    assert client.post(f"/admin/tenancy/tenant/{scenario.a.pk}/activate/").status_code == 403
    assert "tenant_id" not in client.session
