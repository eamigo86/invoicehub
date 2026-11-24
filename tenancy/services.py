"""Tenancy services: tenant creation, membership listing and tenant selection.

Views and signal handlers call these functions, so the rules about who may enter which tenant
live in one place.
"""

import uuid

from django.contrib.sessions.backends.base import SessionBase
from django.db import transaction
from django.db.models import QuerySet
from django.http import Http404

from accounts.models import User
from tenancy.models import Membership, Tenant


def create_tenant(user: User, name: str) -> Tenant:
    """Create a tenant and make its creator the owner, in one transaction.

    Args:
        user: User who creates the company.
        name: Display name of the new tenant.

    Returns:
        The new tenant.
    """
    with transaction.atomic():
        tenant = Tenant.objects.create(name=name)
        Membership.objects.create(user=user, tenant=tenant, role=Membership.Role.OWNER)
    return tenant


def list_memberships(user: User) -> QuerySet[Membership]:
    """List the memberships of a user, always filtered by that user.

    Args:
        user: User whose memberships are listed.

    Returns:
        The memberships of that user with their tenants, ordered by tenant name.
    """
    return Membership.objects.filter(user=user).select_related("tenant").order_by("tenant__name")


def select_tenant(session: SessionBase, user: User, tenant_id: uuid.UUID) -> Membership:
    """Make a tenant the active one for a session.

    Args:
        session: Session that stores the active tenant.
        user: User who asks for the switch.
        tenant_id: Tenant to switch to.

    Returns:
        The membership that entitles the user to the tenant.

    Raises:
        Http404: When the user has no membership in the tenant, whether or not it exists, so
            that the answer never reveals which tenants exist.
    """
    membership = list_memberships(user).filter(tenant_id=tenant_id).first()
    if membership is None:
        raise Http404("no such tenant")
    session["tenant_id"] = str(tenant_id)
    return membership


def select_single_tenant(session: SessionBase, user: User) -> None:
    """Select the tenant of a user who belongs to exactly one, and nothing otherwise.

    Args:
        session: Session that stores the active tenant.
        user: User who has just signed in.
    """
    memberships = list(list_memberships(user)[:2])
    if len(memberships) == 1:
        session["tenant_id"] = str(memberships[0].tenant_id)
