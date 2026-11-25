"""Management command for operators and CI: issue an API token on behalf of a member."""

from typing import Any
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser

from tenancy.models import Membership
from tenancy.permissions import Principal
from tenancy.tokens import issue_token


def _memberships(email: str, tenant: str) -> list[Membership]:
    """Find the memberships of an active user in the tenants that match a name or an id.

    Args:
        email: Email of the member, compared without regard to case.
        tenant: Tenant name or tenant id.

    Returns:
        The matching memberships, empty when there are none.
    """
    found = Membership.objects.filter(
        user__email__iexact=email, user__is_active=True
    ).select_related("user")
    try:
        return list(found.filter(tenant_id=UUID(tenant)))
    except ValueError:
        return list(found.filter(tenant__name=tenant))


class Command(BaseCommand):
    """Issue an API token with the permissions of a member and print its plaintext once."""

    help = (
        "Issue an API token for a tenant member and print the plaintext, which is never shown "
        "again. The member needs the right to manage tokens."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        """Declare the command options.

        Args:
            parser: Parser to add the options to.
        """
        parser.add_argument("--user", required=True, help="Email of the member who asks.")
        parser.add_argument("--tenant", required=True, help="Tenant name or id.")
        parser.add_argument("--name", required=True, help="Display name of the token.")
        parser.add_argument(
            "--scope",
            action="append",
            required=True,
            help="Scope to grant, for example invoices:read; repeat for several.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Issue the token and print its plaintext, and nothing else, to standard output.

        Args:
            *args: Positional arguments, unused.
            **options: Parsed options.

        Raises:
            CommandError: When the user is not a member of the tenant, the email or name
                matches more than one member or tenant, the member may not manage tokens, or the
                token is invalid.
        """
        found = _memberships(options["user"], options["tenant"])
        if not found:
            raise CommandError(
                f"There is no member {options['user']} in tenant {options['tenant']}."
            )
        if len(found) > 1:
            raise CommandError(
                f"{options['user']} in {options['tenant']} matches more than one member or tenant; "
                "pass the tenant id and an exact email."
            )
        membership = found[0]
        try:
            _, plaintext = issue_token(
                Principal.from_membership(membership),
                options["name"],
                options["scope"],
                created_by=membership.user,
            )
        except PermissionDenied:
            raise CommandError(
                f"{membership.user.email} is not allowed to manage tokens in this tenant."
            ) from None
        except ValidationError as error:
            raise CommandError(" ".join(error.messages)) from error
        self.stdout.write(plaintext)
