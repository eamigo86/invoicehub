"""The single permission service: roles and API token scopes both resolve to permissions."""

import uuid
from dataclasses import dataclass
from enum import StrEnum

from django.core.exceptions import PermissionDenied

from tenancy.models import Membership


class Permission(StrEnum):
    """An action a principal may be allowed to perform inside its tenant."""

    INVOICES_READ = "invoices:read"
    INVOICES_WRITE = "invoices:write"
    RECONCILIATION_WRITE = "reconciliation:write"
    TOKENS_MANAGE = "tokens:manage"


ROLE_PERMISSIONS: dict[Membership.Role, frozenset[Permission]] = {
    Membership.Role.OWNER: frozenset(Permission),
    Membership.Role.ADMIN: frozenset(Permission),
    Membership.Role.ACCOUNTANT: frozenset(Permission) - {Permission.TOKENS_MANAGE},
    Membership.Role.VIEWER: frozenset({Permission.INVOICES_READ}),
}


@dataclass(frozen=True)
class Principal:
    """Who is acting in a tenant and what they may do there.

    Attributes:
        tenant_id: Tenant the actor acts in.
        permissions: Permissions the actor holds in that tenant.
    """

    tenant_id: uuid.UUID
    permissions: frozenset[Permission]

    @classmethod
    def from_membership(cls, membership: Membership) -> "Principal":
        """Build the principal of a member from the permissions of their role.

        Args:
            membership: Membership of the acting user.

        Returns:
            A principal for the membership tenant with the permissions of its role.
        """
        return cls(membership.tenant_id, ROLE_PERMISSIONS[Membership.Role(membership.role)])


def has_permission(principal: Principal | None, permission: Permission) -> bool:
    """Tell whether a principal holds a permission.

    Args:
        principal: Acting principal, or None when there is no tenant or actor.
        permission: Permission to check.

    Returns:
        True when the principal holds the permission, False otherwise and for None.
    """
    return principal is not None and permission in principal.permissions


def require_permission(principal: Principal | None, permission: Permission) -> None:
    """Refuse the request unless the principal holds a permission.

    Args:
        principal: Acting principal, or None when there is no tenant or actor.
        permission: Permission that is required.

    Raises:
        PermissionDenied: When the principal does not hold the permission.
    """
    if not has_permission(principal, permission):
        raise PermissionDenied(f"missing permission {permission}")
