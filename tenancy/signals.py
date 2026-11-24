"""Signal handlers of the tenancy app, connected when the app is ready."""

from typing import Any

from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver
from django.http import HttpRequest

from accounts.models import User
from tenancy.services import select_single_tenant


@receiver(user_logged_in)
def select_tenant_on_login(
    sender: object, request: HttpRequest | None, user: User, **kwargs: Any
) -> None:
    """Select the tenant of a user who has exactly one membership right after sign in.

    Args:
        sender: Class of the signed in user, ignored.
        request: Request that signed the user in, if there is one.
        user: User who signed in.
        **kwargs: Other signal arguments, ignored.
    """
    if request is not None:
        select_single_tenant(request.session, user)
