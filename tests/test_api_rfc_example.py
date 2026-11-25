"""The integrator query of RFC 0004 stays valid against the schema and within the limits."""

import re
from pathlib import Path

import pytest
from graphql import parse, validate

from api.schema import schema
from tests.test_api_queries import data, make_world

pytestmark = pytest.mark.django_db(transaction=True)

RFC = Path(__file__).resolve().parents[1] / "docs" / "rfc" / "0004-api-ui.md"


def documented_query() -> str:
    """Extract the GraphQL block under the example integrator query heading.

    Returns:
        The query text.
    """
    text = RFC.read_text(encoding="utf-8")
    section = text.split("### Example integrator query", 1)[1]
    match = re.search(r"```graphql\n(.*?)```", section, re.DOTALL)
    assert match, "the RFC has no graphql block under the example integrator query"
    return match.group(1)


def test_the_rfc_example_is_valid_against_the_schema() -> None:
    """The documented query uses fields, arguments and enum values the schema has."""
    errors = validate(schema.graphql_schema, parse(documented_query()))
    assert [e.message for e in errors] == []


def test_the_rfc_example_runs_within_the_depth_and_cost_limits() -> None:
    """The API accepts the documented query, so depth and cost guards do not refuse it."""
    token = make_world("A", ["invoices:read"])
    result = data(documented_query(), token)
    assert "results" in result["invoices"]


def test_the_rfc_example_with_a_full_page_stays_within_the_limits() -> None:
    """The largest page the API serves is still accepted for the documented shape."""
    token = make_world("A", ["invoices:read"])
    query = documented_query().replace("first: 50", "first: 100")
    assert "results" in data(query, token)["invoices"]


def test_the_rfc_does_not_call_its_syntax_provisional() -> None:
    """The note saying that the syntax is provisional is gone."""
    assert "updated once the schema is settled" not in RFC.read_text(encoding="utf-8")
