"""API protection: query depth and cost limits, introspection and per-token rate limits."""

from typing import Any

import pytest
from django_graphex.settings import graphql_api_settings

from tenancy.models import ApiToken, Membership, Tenant
from tenancy.permissions import Principal
from tenancy.tokens import issue_token
from tests.test_api_graphql import make_world, post
from tests.test_api_queries import NESTED

pytestmark = pytest.mark.django_db(transaction=True)

# Thirty aliased copies of the RFC overdue-invoices shape, each with a full page of 100.
ALIASED = (
    "{ "
    + " ".join(
        f"a{i}: invoices {{ results(first: 100) {{ reference customer {{ name }} "
        "lines { totalCount results { position total } } "
        "allocations { totalCount results { amount payment { reference } } } } } "
        for i in range(30)
    )
    + " }"
)


def test_the_guards_are_configured() -> None:
    """Depth and cost limits are on, so graphex rejects what exceeds them."""
    assert graphql_api_settings.MAX_QUERY_DEPTH is not None
    assert graphql_api_settings.MAX_QUERY_COST is not None


def test_the_rfc_overdue_query_is_within_the_limits() -> None:
    """Customer, lines and allocations of a full page pass the depth and cost guards."""
    _, token = make_world("A", ["invoices:read"])
    assert post(NESTED, token).status_code == 200


def test_a_query_deeper_than_the_limit_is_a_client_error(settings: Any) -> None:
    """Over-deep nesting is a 400 with a depth message, not an internal error.

    The schema has no cycle and its deepest path is five levels, so the limit is lowered here.

    Args:
        settings: Pytest-django settings fixture.
    """
    settings.DJANGO_GRAPHEX = {**settings.DJANGO_GRAPHEX, "MAX_QUERY_DEPTH": 3}
    _, token = make_world("A", ["invoices:read"])
    response = post(NESTED, token)
    assert response.status_code == 400
    assert "depth" in response.json()["errors"][0]["message"].lower()


def test_alias_amplification_over_the_cost_limit_is_a_client_error() -> None:
    """Thirty aliased full pages exceed the cost budget with a 400."""
    _, token = make_world("A", ["invoices:read"])
    response = post(ALIASED, token)
    assert response.status_code == 400
    assert "cost" in response.json()["errors"][0]["message"].lower()


INTROSPECTION = "{ __schema { queryType { name } } }"


def dev_token(tenant: Tenant) -> str:
    """Issue a development token, one that may introspect, in a tenant.

    Args:
        tenant: Tenant that already has an owner membership.

    Returns:
        The plaintext token.
    """
    membership = Membership.objects.get(tenant=tenant)
    token, plaintext = issue_token(
        Principal.from_membership(membership), "dev", ["invoices:read"], allow_introspection=True
    )
    assert token.allow_introspection
    return plaintext


def test_a_token_is_not_a_development_token_by_default() -> None:
    """The flag is opt-in at issue time."""
    tenant, _ = make_world("A", ["invoices:read"])
    assert not ApiToken.objects.get(tenant=tenant).allow_introspection


def test_introspection_is_refused_for_an_ordinary_token() -> None:
    """Outside DEBUG a normal token cannot read the schema, but __typename still works."""
    _, token = make_world("A", ["invoices:read"])
    body = post(INTROSPECTION, token).json()
    assert body["errors"][0]["extensions"]["code"] == "INTROSPECTION_DISABLED"
    assert post("{ __typename }", token).json() == {"data": {"__typename": "Query"}}


def test_a_development_token_may_introspect() -> None:
    """The flag opens introspection for that token only."""
    tenant, ordinary = make_world("A", ["invoices:read"])
    body = post(INTROSPECTION, dev_token(tenant)).json()
    assert body == {"data": {"__schema": {"queryType": {"name": "Query"}}}}
    assert "errors" in post(INTROSPECTION, ordinary).json()


def test_introspection_is_open_in_debug(settings: Any) -> None:
    """With DEBUG on, any token may introspect.

    Args:
        settings: Pytest-django settings fixture.
    """
    settings.DEBUG = True
    _, token = make_world("A", ["invoices:read"])
    assert "errors" not in post(INTROSPECTION, token).json()
