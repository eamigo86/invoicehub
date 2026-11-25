"""The issue_api_token command: operators and CI mint a token from a member's role."""

from io import StringIO
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from accounts.models import User
from tenancy.models import ApiToken, Membership, Tenant
from tenancy.tokens import authenticate_token

pytestmark = pytest.mark.django_db(transaction=True)


def make_member(role: Membership.Role, tenant_name: str = "Acme") -> tuple[Tenant, User]:
    """Create a tenant with one member.

    Args:
        role: Role of the member.
        tenant_name: Name of the tenant.

    Returns:
        The tenant and the member.
    """
    tenant = Tenant.objects.create(name=tenant_name)
    user = User.objects.create_user("ana", "ana@example.com", "pw")
    Membership.objects.create(user=user, tenant=tenant, role=role)
    return tenant, user


def run(*args: str) -> str:
    """Run the command and capture what it prints.

    Args:
        *args: Command line options.

    Returns:
        The standard output of the command.
    """
    out = StringIO()
    call_command("issue_api_token", *args, stdout=out)
    return out.getvalue()


def options(user: str = "ana@example.com", tenant: str = "Acme") -> list[str]:
    """Build the options of a valid call.

    Args:
        user: Email of the member.
        tenant: Tenant name or id.

    Returns:
        The command line options.
    """
    return [
        "--user",
        user,
        "--tenant",
        tenant,
        "--scope",
        "invoices:read",
        "--scope",
        "invoices:write",
        "--name",
        "shop",
    ]


def test_an_owner_gets_a_working_token_and_nothing_else_is_printed() -> None:
    """Only the plaintext is printed, and it authenticates with the requested scopes."""
    tenant, user = make_member(Membership.Role.OWNER)
    output = run(*options())
    plaintext = output.strip()
    assert output == plaintext + "\n"
    token = authenticate_token(plaintext)
    assert token is not None
    assert token.tenant_id == tenant.pk
    assert token.scopes == ["invoices:read", "invoices:write"]
    assert token.name == "shop"
    assert token.created_by == user
    assert token.allow_introspection is False


def test_the_tenant_can_be_given_by_id() -> None:
    """A tenant id works as well as a tenant name."""
    tenant, _ = make_member(Membership.Role.ADMIN)
    assert authenticate_token(run(*options(tenant=str(tenant.pk))).strip()) is not None


def test_a_viewer_is_refused_and_no_token_exists() -> None:
    """A member who may not manage tokens gets an error and no token is stored."""
    make_member(Membership.Role.VIEWER)
    with pytest.raises(CommandError, match="not allowed"):
        run(*options())
    assert ApiToken.objects.count() == 0


@pytest.mark.parametrize(
    ("user", "tenant"),
    [
        ("nobody@example.com", "Acme"),
        ("ana@example.com", "Other"),
        ("ana@example.com", str(uuid4())),
    ],
)
def test_an_unknown_membership_is_refused(user: str, tenant: str) -> None:
    """A user who is not a member of the tenant gets an error, whichever part is wrong.

    Args:
        user: Email of the requested member.
        tenant: Requested tenant name or id.
    """
    make_member(Membership.Role.OWNER)
    with pytest.raises(CommandError, match="no member"):
        run(*options(user=user, tenant=tenant))


def test_a_deactivated_user_is_refused_like_an_unknown_member() -> None:
    """A user who can no longer sign in cannot mint tokens either."""
    _, user = make_member(Membership.Role.OWNER)
    User.objects.filter(pk=user.pk).update(is_active=False)
    with pytest.raises(CommandError, match="no member"):
        run(*options())
    assert ApiToken.objects.count() == 0


def test_an_accountant_is_refused_because_it_cannot_manage_tokens() -> None:
    """The accountant role lacks tokens:manage, so no token is stored."""
    make_member(Membership.Role.ACCOUNTANT)
    with pytest.raises(CommandError, match="not allowed"):
        run(*options())
    assert ApiToken.objects.count() == 0


def test_a_member_of_another_tenant_cannot_mint_for_this_one() -> None:
    """Being an owner elsewhere grants nothing in a tenant the user does not belong to."""
    make_member(Membership.Role.OWNER, tenant_name="Other")
    Tenant.objects.create(name="Acme")
    with pytest.raises(CommandError, match="no member"):
        run(*options())
    assert ApiToken.objects.count() == 0


def test_a_name_shared_by_two_tenants_is_ambiguous() -> None:
    """A tenant name that matches two tenants of the user is refused instead of guessed."""
    _, user = make_member(Membership.Role.OWNER)
    second = Tenant.objects.create(name="Acme")
    Membership.objects.create(user=user, tenant=second, role=Membership.Role.OWNER)
    with pytest.raises(CommandError, match="more than one"):
        run(*options())


def test_two_users_differing_only_in_email_case_are_ambiguous() -> None:
    """The email lookup ignores case, so two such members of one tenant are refused."""
    tenant, _ = make_member(Membership.Role.OWNER)
    twin = User.objects.create_user("ANA", "ANA@example.com", "pw")
    Membership.objects.create(user=twin, tenant=tenant, role=Membership.Role.OWNER)
    with pytest.raises(CommandError, match="more than one member or tenant"):
        run(*options())
    assert ApiToken.objects.count() == 0


def test_an_invalid_scope_is_a_command_error() -> None:
    """The validation of the token service is reported as a command error."""
    make_member(Membership.Role.OWNER)
    with pytest.raises(CommandError, match="Scopes"):
        run(
            "--user",
            "ana@example.com",
            "--tenant",
            "Acme",
            "--scope",
            "tokens:manage",
            "--name",
            "x",
        )
