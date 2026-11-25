"""The audit event admin: read-only, and readable actor names."""

import uuid

import pytest
from django.contrib import admin
from django.test import RequestFactory

from accounts.models import User
from billing.admin import AuditEventAdmin
from billing.models import AuditEvent
from billing.services import record_event
from tenancy.context import tenant_context
from tenancy.models import ApiToken, Tenant
from tenancy.permissions import Principal

pytestmark = pytest.mark.django_db


def test_the_actor_column_names_the_user_the_token_or_falls_back_to_the_kind() -> None:
    """The user name or token name is shown, and the kind once the actor is gone."""
    tenant = Tenant.objects.create(name="Acme")
    user = User.objects.create_user("ann", "ann@example.com", "pw")
    token = ApiToken.objects.create(tenant=tenant, name="ci", token_hash="h" * 64, scopes=[])
    model_admin = AuditEventAdmin(AuditEvent, admin.site)
    with tenant_context(tenant.pk):
        by_user = record_event(
            Principal(tenant.pk, frozenset(), user_id=user.pk), "a", "invoice", uuid.uuid4()
        )
        by_token = record_event(Principal.from_token(token), "a", "invoice", uuid.uuid4())
        by_system = record_event(None, "a", "invoice", uuid.uuid4())
        assert model_admin.actor(by_user) == "ann"
        assert model_admin.actor(by_token) == "ci"
        assert model_admin.actor(by_system) == "System"


def test_the_admin_allows_no_write_to_anyone() -> None:
    """Add, change and delete are refused even to a superuser."""
    request = RequestFactory().get("/")
    request.user = User(is_active=True, is_superuser=True)
    model_admin = AuditEventAdmin(AuditEvent, admin.site)
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request)
    assert not model_admin.has_delete_permission(request)
