"""The append-only audit log and its single write path, record_event."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied

from accounts.models import User
from billing.models import AuditEvent
from billing.services import record_event
from tenancy.context import TenantNotSet, tenant_context
from tenancy.models import ApiToken, Tenant
from tenancy.permissions import Permission, Principal

pytestmark = pytest.mark.django_db


@pytest.fixture
def tenant() -> Tenant:
    """Create a tenant.

    Returns:
        The tenant.
    """
    return Tenant.objects.create(name="Acme")


def test_a_user_principal_is_recorded_as_a_user_actor(tenant: Tenant) -> None:
    """The event names the user and carries the entity, the action and the tenant.

    Args:
        tenant: Tenant the event belongs to.
    """
    user = User.objects.create_user("ann", "ann@example.com", "pw")
    entity_id = uuid.uuid4()
    principal = Principal(tenant.pk, frozenset({Permission.INVOICES_READ}), user_id=user.pk)
    with tenant_context(tenant.pk):
        event = record_event(principal, "invoice.issued", "invoice", entity_id)
        stored = AuditEvent.objects.get()
    assert stored == event
    assert (stored.actor_kind, stored.actor_user_id, stored.actor_token_id) == (
        AuditEvent.ActorKind.USER,
        user.pk,
        None,
    )
    assert (stored.action, stored.entity, stored.entity_id) == (
        "invoice.issued",
        "invoice",
        entity_id,
    )
    assert (stored.tenant_id, stored.before, stored.after) == (tenant.pk, None, None)
    assert stored.created_at is not None


def test_a_token_principal_is_recorded_as_a_token_actor(tenant: Tenant) -> None:
    """A principal with a token id is a token actor.

    Args:
        tenant: Tenant the event belongs to.
    """
    token = ApiToken.objects.create(
        tenant=tenant, name="ci", token_hash="h" * 64, scopes=["invoices:write"]
    )
    principal = Principal.from_token(token)
    with tenant_context(tenant.pk):
        event = record_event(principal, "invoice.sent", "invoice", uuid.uuid4())
    assert (event.actor_kind, event.actor_token_id, event.actor_user_id) == (
        AuditEvent.ActorKind.TOKEN,
        token.pk,
        None,
    )


def test_no_principal_is_recorded_as_the_system(tenant: Tenant) -> None:
    """Work with no acting principal, such as a task, is attributed to the system.

    Args:
        tenant: Tenant the event belongs to.
    """
    with tenant_context(tenant.pk):
        event = record_event(None, "invoice.sent", "invoice", uuid.uuid4())
    assert (event.actor_kind, event.actor_user_id, event.actor_token_id) == (
        AuditEvent.ActorKind.SYSTEM,
        None,
        None,
    )


def test_before_and_after_are_stored_as_json(tenant: Tenant) -> None:
    """Values such as decimals and dates are encoded instead of failing.

    Args:
        tenant: Tenant the event belongs to.
    """
    with tenant_context(tenant.pk):
        record_event(
            None,
            "invoice.updated",
            "invoice",
            uuid.uuid4(),
            before={"total": Decimal("1.50")},
            after={"due": date(2026, 12, 1)},
        )
        stored = AuditEvent.objects.get()
    assert (stored.before, stored.after) == ({"total": "1.50"}, {"due": "2026-12-01"})


def test_events_are_listed_newest_first(tenant: Tenant) -> None:
    """The default ordering puts the latest event on top.

    Args:
        tenant: Tenant the events belong to.
    """
    with tenant_context(tenant.pk):
        first = record_event(None, "a", "invoice", uuid.uuid4())
        second = record_event(None, "b", "invoice", uuid.uuid4())
        assert list(AuditEvent.objects.all()) == [second, first]


def test_the_event_outlives_the_user_it_names(tenant: Tenant) -> None:
    """Deleting the user keeps the event and clears the reference.

    Args:
        tenant: Tenant the event belongs to.
    """
    user = User.objects.create_user("ann", "ann@example.com", "pw")
    principal = Principal(tenant.pk, frozenset(), user_id=user.pk)
    with tenant_context(tenant.pk):
        record_event(principal, "invoice.issued", "invoice", uuid.uuid4())
        user.delete()
        stored = AuditEvent.objects.get()
    assert (stored.actor_kind, stored.actor_user_id) == (AuditEvent.ActorKind.USER, None)


def test_a_principal_of_another_tenant_is_refused(tenant: Tenant) -> None:
    """The principal must belong to the current tenant and nothing is stored otherwise.

    Args:
        tenant: Tenant that is current.
    """
    other = Tenant.objects.create(name="Other")
    with tenant_context(tenant.pk):
        with pytest.raises(PermissionDenied):
            record_event(Principal(other.pk, frozenset()), "a", "invoice", uuid.uuid4())
        assert AuditEvent.objects.count() == 0


def test_recording_needs_an_active_tenant() -> None:
    """With no tenant active the event cannot be recorded."""
    with pytest.raises(TenantNotSet):
        record_event(None, "a", "invoice", uuid.uuid4())
