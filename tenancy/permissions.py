"""The single permission service: roles and API token scopes both resolve to permissions."""

import uuid
from dataclasses import dataclass
from enum import StrEnum

from django.core.exceptions import PermissionDenied
from django.http import HttpRequest

from tenancy.context import get_current_tenant_id
from tenancy.models import ApiToken, Membership


class Permission(StrEnum):
    """An action a principal may be allowed to perform inside its tenant."""

    INVOICES_READ = "invoices:read"
    INVOICES_WRITE = "invoices:write"
    RECONCILIATION_WRITE = "reconciliation:write"
    TOKENS_MANAGE = "tokens:manage"
    SETTINGS_MANAGE = "settings:manage"


ROLE_PERMISSIONS: dict[Membership.Role, frozenset[Permission]] = {
    Membership.Role.OWNER: frozenset(Permission),
    Membership.Role.ADMIN: frozenset(Permission),
    Membership.Role.ACCOUNTANT: frozenset(
        {Permission.INVOICES_READ, Permission.INVOICES_WRITE, Permission.RECONCILIATION_WRITE}
    ),
    Membership.Role.VIEWER: frozenset({Permission.INVOICES_READ}),
}


TOKEN_SCOPES: frozenset[Permission] = frozenset(
    {Permission.INVOICES_READ, Permission.INVOICES_WRITE, Permission.RECONCILIATION_WRITE}
)
"""Permissions an API token may carry; tokens:manage is never a token scope."""


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

    @classmethod
    def from_token(cls, token: ApiToken) -> "Principal":
        """Build the principal of an API token from its scopes alone.

        The creator's role is deliberately ignored, so a token never outgrows its scopes.

        Args:
            token: Active API token.

        Returns:
            A principal for the token tenant holding exactly the token scopes.
        """
        return cls(token.tenant_id, frozenset(Permission(scope) for scope in token.scopes))


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
    """Refuse the request unless the principal holds a permission in the active tenant.

    When a tenant is active, the principal must belong to it, so a principal of one tenant can
    never act through the services while another tenant is current.

    Args:
        principal: Acting principal, or None when there is no tenant or actor.
        permission: Permission that is required.

    Raises:
        PermissionDenied: When the principal does not hold the permission, or belongs to a
            tenant other than the active one.
    """
    if not has_permission(principal, permission):
        raise PermissionDenied(f"missing permission {permission}")
    assert principal is not None
    active = get_current_tenant_id()
    if active is not None and principal.tenant_id != active:
        raise PermissionDenied("principal belongs to another tenant")


_PRINCIPAL_ATTR = "tenant_principal"


def set_request_principal(request: HttpRequest, principal: Principal | None) -> None:
    """Attach the acting principal to a request.

    Stored under a private attribute name so that the typed accessors are the only readers.

    Args:
        request: Request that is being served.
        principal: Principal of the member acting in the active tenant, or None.
    """
    setattr(request, _PRINCIPAL_ATTR, principal)


def request_principal(request: HttpRequest) -> Principal | None:
    """Read the principal attached to a request.

    Args:
        request: Request that is being served.

    Returns:
        The principal set by the tenant middleware, or None when none was set or the request
        has no tenant or member.
    """
    principal: Principal | None = getattr(request, _PRINCIPAL_ATTR, None)
    return principal
