"""API protection: query depth and cost limits, introspection and per-token rate limits."""

from typing import Any

import pytest
from django_graphex.settings import graphql_api_settings

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
