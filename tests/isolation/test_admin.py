"""Every admin of a tenant model only shows and changes the active tenant's rows.

Cases run for each admin registered for a TenantModel, using the factory registry to create
rows. Real transactions are required: the tenant reaches PostgreSQL when a request's outermost
transaction begins.
"""

import uuid
from typing import Any

import pytest
from django.contrib import admin
from django.test import Client, RequestFactory
from django.urls import reverse

from accounts.models import User
from tenancy.context import tenant_context
from tenancy.models import Membership, Tenant, TenantModel
from tenancy.permissions import Permission, Principal, set_request_principal
from tests.isolation.registry import (
    ADMIN_MARKER_FIELDS,
    FACTORIES,
    PARTIAL_ADMINS,
    READ_ONLY_ADMINS,
)

pytestmark = [pytest.mark.isolation, pytest.mark.django_db(transaction=True)]


def tenant_admin_models() -> list[type[TenantModel]]:
    """List the tenant models that have an admin registered.

    Returns:
        The model classes, ordered by name.
    """
    found = [m for m in admin.site._registry if issubclass(m, TenantModel) and not m._meta.abstract]
    return sorted(found, key=lambda m: m.__name__)


def admin_allowed_writes(model_admin: "admin.ModelAdmin[Any]") -> frozenset[str]:
    """List which of add, change and delete an admin allows a superuser holding everything.

    Args:
        model_admin: Admin instance to probe, inside a tenant so the tenant guard is not what
            refuses.

    Returns:
        The allowed operations among add, change and delete.
    """
    request = RequestFactory().get("/")
    request.user = User(is_active=True, is_superuser=True)
    set_request_principal(request, Principal(uuid.uuid4(), frozenset(Permission)))
    with tenant_context(uuid.uuid4()):
        checks = {
            "add": model_admin.has_add_permission(request),
            "change": model_admin.has_change_permission(request),
            "delete": model_admin.has_delete_permission(request),
        }
    return frozenset(name for name, allowed in checks.items() if allowed)


def registered_writes(model: type[TenantModel]) -> frozenset[str]:
    """List the writes the registries say a model's admin allows.

    Args:
        model: Tenant model with an admin.

    Returns:
        Nothing for READ_ONLY_ADMINS, the listed operations for PARTIAL_ADMINS, otherwise add,
        change and delete.
    """
    if model in READ_ONLY_ADMINS:
        return frozenset()
    return PARTIAL_ADMINS.get(model, frozenset({"add", "change", "delete"}))


def admin_is_read_only(model_admin: "admin.ModelAdmin[Any]") -> bool:
    """Tell whether an admin refuses add, change and delete to a superuser holding everything.

    Args:
        model_admin: Admin instance to probe, inside a tenant so the tenant guard is not what
            refuses.

    Returns:
        True when none of add, change and delete is allowed.
    """
    return not admin_allowed_writes(model_admin)


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
    """Two tenants owning one row each of a model, and a client logged in as owner of both.

    Attributes:
        model: Tenant model under test.
        a: Tenant A.
        b: Tenant B.
        a_row: Row owned by A.
        b_row: Row owned by B.
        client: Logged in test client with no session tenant yet.
        root: Superuser behind the client, an owner of both tenants.
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
        self.root = User.objects.create_superuser("root", "root@example.com", "pw")
        for tenant in (self.a, self.b):
            Membership.objects.create(user=self.root, tenant=tenant, role=Membership.Role.OWNER)
        client.force_login(self.root)

    def login_as(self, role: Membership.Role, tenant: Tenant) -> None:
        """Log in as a new superuser holding a single membership.

        Args:
            role: Role of the membership.
            tenant: Tenant the user belongs to.
        """
        user = User.objects.create_superuser(f"{role}-user", f"{role}@example.com", "pw")
        Membership.objects.create(user=user, tenant=tenant, role=role)
        self.client.force_login(user)

    @property
    def allowed(self) -> frozenset[str]:
        """List the writes the admin under test is registered to allow.

        Returns:
            Nothing for READ_ONLY_ADMINS, the listed operations for PARTIAL_ADMINS, otherwise
            add, change and delete.
        """
        return registered_writes(self.model)

    def needle(self, row: TenantModel) -> str:
        """Pick text that identifies a row on a rendered admin page.

        Args:
            row: Row to identify.

        Returns:
            The value of the model's marker field, which is unique per row.
        """
        return str(getattr(row, ADMIN_MARKER_FIELDS.get(self.model, "name")))

    def writer_role(self) -> Membership.Role:
        """Pick the least privileged role that holds the admin's write permission.

        Returns:
            Admin for admins that need settings:manage, otherwise accountant.
        """
        needed = admin.site._registry[self.model].write_permission  # type: ignore[attr-defined]
        return (
            Membership.Role.ADMIN
            if needed == Permission.SETTINGS_MANAGE
            else Membership.Role.ACCOUNTANT
        )

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

    def hostile_data(self) -> dict[str, str | list[str]]:
        """Build a valid change form POST for A's row that hijacks one designated text field.

        The data starts from what the admin's own change form renders for the row, so it is
        valid whatever the field types, and only the field named in ADMIN_MARKER_FIELDS is
        overridden with the marker "hijacked".

        Returns:
            Field values as the browser would post them.
        """
        reader = Client()
        reader.force_login(self.root)
        session = reader.session
        session["tenant_id"] = str(self.a.pk)
        session.save()
        form = reader.get(self.url("change", self.a_row)).context["adminform"].form
        data: dict[str, str | list[str]] = {}
        for name in form.fields:
            value = form[name].value()
            if isinstance(value, list):
                data[name] = [str(v) for v in value]
            elif value is True:
                data[name] = "on"
            elif value is not None and value is not False:
                data[name] = str(value)
        data[ADMIN_MARKER_FIELDS.get(self.model, "name")] = "hijacked"
        return data

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


def test_the_write_registries_match_the_admins() -> None:
    """Every admin allows exactly the writes its registry entry says.

    A restricted admin missing from READ_ONLY_ADMINS or PARTIAL_ADMINS would have its write
    cases fail loudly instead of being skipped, and a stale entry would hide a writable admin.
    """
    registered = set(tenant_admin_models())
    assert registered >= READ_ONLY_ADMINS | PARTIAL_ADMINS.keys()
    for model in registered:
        assert admin_allowed_writes(admin.site._registry[model]) == registered_writes(model), model


def test_the_read_only_probe_tells_writable_and_read_only_admins_apart() -> None:
    """The probe is False for a default admin and True once the write permissions are refused."""

    class Locked(admin.ModelAdmin):  # type: ignore[type-arg]
        """Admin that refuses every write."""

        def has_add_permission(self, request: Any) -> bool:
            """Refuse adding.

            Args:
                request: Current request.

            Returns:
                False.
            """
            return False

        def has_change_permission(self, request: Any, obj: Any = None) -> bool:
            """Refuse changing.

            Args:
                request: Current request.
                obj: Object being changed, if any.

            Returns:
                False.
            """
            return False

        def has_delete_permission(self, request: Any, obj: Any = None) -> bool:
            """Refuse deleting.

            Args:
                request: Current request.
                obj: Object being deleted, if any.

            Returns:
                False.
            """
            return False

    model = next(iter(FACTORIES))
    assert not admin_is_read_only(admin.ModelAdmin(model, admin.site))
    assert admin_is_read_only(Locked(model, admin.site))


def test_changelist_shows_only_the_active_tenants_rows(scenario: Scenario) -> None:
    """A's row is listed and B's is not.

    Args:
        scenario: Admin scenario for one tenant model.
    """
    scenario.activate(scenario.a.pk)
    response = scenario.client.get(scenario.url("changelist"))
    assert listed_pks(response) == [scenario.a_row.pk]
    body = response.content.decode()
    assert scenario.needle(scenario.a_row) in body
    assert scenario.needle(scenario.b_row) not in body


@pytest.mark.parametrize("kind", ["change", "delete", "history"])
def test_foreign_object_views_do_not_render(scenario: Scenario, kind: str) -> None:
    """B's change, delete and history pages are not reachable and never show B's data.

    An admin without delete answers 403 to the delete page, because Django checks the delete
    permission before it looks the object up; every other page redirects.

    Args:
        scenario: Admin scenario for one tenant model.
        kind: Admin URL name suffix.
    """
    scenario.activate(scenario.a.pk)
    url = scenario.url(kind, scenario.b_row)
    expected = 403 if "delete" not in scenario.allowed and kind == "delete" else 302
    assert scenario.client.get(url).status_code == expected
    body = scenario.client.get(url, follow=True).content.decode()
    assert scenario.needle(scenario.b_row) not in body


def test_posts_to_foreign_rows_change_nothing_but_own_rows_still_work(scenario: Scenario) -> None:
    """Change and delete POSTs aimed at B leave it untouched, while the same POSTs work for A.

    For each write the admin does not allow, the POST aimed at A is refused too and A's row is
    left unchanged.

    Args:
        scenario: Admin scenario for one tenant model.
    """
    before = scenario.snapshot(scenario.b, scenario.b_row)
    scenario.activate(scenario.a.pk)
    data = scenario.hostile_data()
    scenario.client.post(scenario.url("change", scenario.b_row), data)
    scenario.client.post(scenario.url("delete", scenario.b_row), {"post": "yes"})
    assert scenario.snapshot(scenario.b, scenario.b_row) == before
    own_before = scenario.snapshot(scenario.a, scenario.a_row)
    change = scenario.client.post(scenario.url("change", scenario.a_row), data)
    changed = scenario.snapshot(scenario.a, scenario.a_row)
    if "change" in scenario.allowed:
        assert changed is not None
        assert "hijacked" in {str(v) for v in changed.values()}
    else:
        assert change.status_code == 403
        assert changed == own_before
    delete = scenario.client.post(scenario.url("delete", scenario.a_row), {"post": "yes"})
    if "delete" in scenario.allowed:
        assert scenario.snapshot(scenario.a, scenario.a_row) is None
    else:
        assert delete.status_code == 403
        assert scenario.snapshot(scenario.a, scenario.a_row) == changed


def test_admin_is_hidden_without_a_session_tenant(scenario: Scenario) -> None:
    """With no tenant in the session the admin of each tenant model is not accessible.

    Args:
        scenario: Admin scenario for one tenant model.
    """
    assert scenario.client.get(scenario.url("changelist")).status_code == 403


def test_member_of_another_tenant_cannot_reach_a_forged_session_tenant(scenario: Scenario) -> None:
    """A member of A whose session names B sees nothing of B and the key is dropped.

    Args:
        scenario: Admin scenario for one tenant model.
    """
    scenario.login_as(Membership.Role.OWNER, scenario.a)
    scenario.activate(scenario.b.pk)
    assert scenario.client.get(scenario.url("changelist")).status_code == 403
    assert scenario.client.get(scenario.url("change", scenario.b_row)).status_code == 403
    assert "tenant_id" not in scenario.client.session
    assert scenario.snapshot(scenario.b, scenario.b_row) is not None


def test_viewer_can_view_but_not_write(scenario: Scenario) -> None:
    """A viewer lists and opens rows but cannot add, change or delete them.

    Args:
        scenario: Admin scenario for one tenant model.
    """
    scenario.login_as(Membership.Role.VIEWER, scenario.a)
    scenario.activate(scenario.a.pk)
    before = scenario.snapshot(scenario.a, scenario.a_row)
    assert scenario.client.get(scenario.url("changelist")).status_code == 200
    assert scenario.client.get(scenario.url("change", scenario.a_row)).status_code == 200
    assert scenario.client.get(scenario.url("add")).status_code == 403
    data = scenario.hostile_data()
    assert scenario.client.post(scenario.url("add"), data).status_code == 403
    assert scenario.client.post(scenario.url("change", scenario.a_row), data).status_code == 403
    delete = scenario.url("delete", scenario.a_row)
    assert scenario.client.post(delete, {"post": "yes"}).status_code == 403
    assert scenario.snapshot(scenario.a, scenario.a_row) == before


def test_a_writer_can_write(scenario: Scenario) -> None:
    """A role holding the admin's write permission keeps full access to the tenant model.

    Writes the admin does not allow are refused even to that role, and the row survives.

    Args:
        scenario: Admin scenario for one tenant model.
    """
    scenario.login_as(scenario.writer_role(), scenario.a)
    scenario.activate(scenario.a.pk)
    before = scenario.snapshot(scenario.a, scenario.a_row)
    add = scenario.client.get(scenario.url("add"))
    assert add.status_code == (200 if "add" in scenario.allowed else 403)
    delete = scenario.client.post(scenario.url("delete", scenario.a_row), {"post": "yes"})
    if "delete" in scenario.allowed:
        assert scenario.snapshot(scenario.a, scenario.a_row) is None
    else:
        assert delete.status_code == 403
        assert scenario.snapshot(scenario.a, scenario.a_row) == before
