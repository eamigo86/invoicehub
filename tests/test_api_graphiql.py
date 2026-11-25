"""The GraphiQL explorer page: gated by one setting, token prefilled, read-only demo token."""

import json
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from tests.test_api_graphql import make_world, post

pytestmark = pytest.mark.django_db(transaction=True)

DEMO_TOKEN = "ih_demo-graphiql-token-for-tests-only-0000000000"  # noqa: S105 - fictitious


def test_the_page_is_not_found_without_the_setting(settings: Any) -> None:
    """With GRAPHIQL_TOKEN empty the explorer does not exist, which is the production default.

    Args:
        settings: Pytest-django settings fixture.
    """
    settings.GRAPHIQL_TOKEN = ""
    assert Client().get(reverse("api:graphiql")).status_code == 404


def test_the_url_is_graphiql(settings: Any) -> None:
    """The named URL lives at /graphiql/.

    Args:
        settings: Pytest-django settings fixture.
    """
    assert reverse("api:graphiql") == "/graphiql/"


def test_the_page_needs_no_login_and_prefills_the_bearer_header(settings: Any) -> None:
    """Anonymous visitors get GraphiQL pointed at /graphql/ with the demo token as Bearer.

    Args:
        settings: Pytest-django settings fixture.
    """
    settings.GRAPHIQL_TOKEN = DEMO_TOKEN
    response = Client().get(reverse("api:graphiql"))
    assert response.status_code == 200
    page = response.content.decode()
    assert f'{{"Authorization": "Bearer {DEMO_TOKEN}"}}' in page
    assert '"/graphql/"' in page


def test_every_asset_is_pinned_with_an_integrity_hash(settings: Any) -> None:
    """Scripts and styles come from versioned URLs and carry an SRI hash.

    Args:
        settings: Pytest-django settings fixture.
    """
    settings.GRAPHIQL_TOKEN = DEMO_TOKEN
    page = Client().get(reverse("api:graphiql")).content.decode()
    for asset in ("react@18.3.1", "react-dom@18.3.1", "graphiql@3.7.1"):
        assert f"https://unpkg.com/{asset}/" in page
    assert page.count('integrity="sha384-') == 4
    assert page.count('src="https://') + page.count('href="https://') == 4


def test_the_page_accepts_only_safe_methods(settings: Any) -> None:
    """A POST to the page is refused.

    Args:
        settings: Pytest-django settings fixture.
    """
    settings.GRAPHIQL_TOKEN = DEMO_TOKEN
    assert Client().post(reverse("api:graphiql")).status_code == 405


def test_a_mutation_with_a_read_only_token_is_denied_in_the_payload() -> None:
    """The scope of the demo token is invoices:read, so a write answers PERMISSION_DENIED."""
    _, token = make_world("A", ["invoices:read"])
    body = post('mutation { saveCustomer(input: {name: "x"}) { errors { code } } }', token).json()
    assert [e["code"] for e in body["data"]["saveCustomer"]["errors"]] == ["PERMISSION_DENIED"]
    assert json.dumps(body).count("PERMISSION_DENIED") == 1
