"""API token services: issue, revoke and authenticate.

Only the sha256 hex of a token is stored. The plaintext exists once, in the value returned by
issue_token.
"""

import hashlib
import secrets

from django.core.exceptions import ValidationError
from django.http import Http404
from django.utils import timezone

from accounts.models import User
from tenancy.models import ApiToken
from tenancy.permissions import TOKEN_SCOPES, Permission, Principal, require_permission

TOKEN_PREFIX = "ih_"


def hash_token(raw: str) -> str:
    """Hash a raw token the way it is stored.

    Args:
        raw: Plaintext token.

    Returns:
        The sha256 hex digest of the token.
    """
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_token(
    principal: Principal | None,
    name: str,
    scopes: list[str],
    created_by: User | None = None,
    allow_introspection: bool = False,
) -> tuple[ApiToken, str]:
    """Issue an API token for the tenant of the acting principal.

    Args:
        principal: Acting principal, who needs tokens:manage or PermissionDenied is raised.
        name: Display name of the token.
        scopes: Scopes to grant, a non-empty subset of the token scopes.
        created_by: User who asks for the token, kept for audit.
        allow_introspection: Whether this is a development token, which may read the GraphQL
            schema even when DEBUG is off. Introspection is disabled in production except for
            development tokens, so never expose this argument on a tenant-facing path.

    Returns:
        The stored token and its plaintext, which is never available again.

    Raises:
        ValidationError: When the name is blank or the scopes are empty or not token scopes.
    """
    require_permission(principal, Permission.TOKENS_MANAGE)
    assert principal is not None
    name = name.strip()
    allowed = {scope.value for scope in TOKEN_SCOPES}
    unique = sorted(set(scopes))
    if not name:
        raise ValidationError("A token needs a name.")
    if not unique or not allowed.issuperset(unique):
        raise ValidationError(f"Scopes must be a non-empty subset of {sorted(allowed)}.")
    plaintext = TOKEN_PREFIX + secrets.token_urlsafe(32)
    token = ApiToken.objects.create(
        tenant_id=principal.tenant_id,
        name=name,
        token_hash=hash_token(plaintext),
        scopes=unique,
        created_by=created_by,
        allow_introspection=allow_introspection,
    )
    return token, plaintext


def revoke_token(principal: Principal | None, token_id: int) -> ApiToken:
    """Revoke a token of the principal's own tenant; revoking twice is harmless.

    Args:
        principal: Acting principal, who needs tokens:manage or PermissionDenied is raised.
        token_id: Id of the token to revoke.

    Returns:
        The revoked token.

    Raises:
        Http404: When the tenant has no such token, whether or not another tenant has it.
    """
    require_permission(principal, Permission.TOKENS_MANAGE)
    assert principal is not None
    token = ApiToken.objects.filter(pk=token_id, tenant_id=principal.tenant_id).first()
    if token is None:
        raise Http404("no such token")
    if token.revoked_at is None:
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
    return token


def authenticate_token(raw: str) -> ApiToken | None:
    """Find the active token behind a raw token string.

    The lookup is by hash, so the plaintext is never compared.

    Args:
        raw: Raw token as presented by a client.

    Returns:
        The active token, or None when the input is unknown, revoked or malformed.
    """
    if not raw.startswith(TOKEN_PREFIX):
        return None
    try:
        digest = hash_token(raw)
    except UnicodeEncodeError:
        return None
    return ApiToken.objects.filter(token_hash=digest, revoked_at__isnull=True).first()
