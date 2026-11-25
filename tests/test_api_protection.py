"""API protection: query depth and cost limits, introspection and per-token rate limits."""

from typing import Any

import pytest
from django.core.cache import cache
from django_graphex.settings import graphql_api_settings
from django_graphex.views import GraphQLView

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


def test_a_full_page_of_the_rfc_query_is_within_the_limits() -> None:
    """The largest page the API serves, with the same nesting, is still accepted."""
    _, token = make_world("A", ["invoices:read"])
    assert post(NESTED.replace("first: 50", "first: 100"), token).status_code == 200


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


def test_the_introspection_choice_keeps_the_configured_middleware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured graphex middleware stays in the chain, with or without the blocker.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    from django.test import RequestFactory

    from api.views import ProtectedGraphQLView
    from tenancy.permissions import set_request_principal

    configured = object()
    monkeypatch.setattr(GraphQLView, "get_middleware", lambda self, request: [configured])
    tenant, _ = make_world("A", ["invoices:read"])
    view = ProtectedGraphQLView(schema=None)
    for allowed in (False, True):
        request = RequestFactory().post("/graphql/")
        set_request_principal(
            request,
            Principal(tenant.pk, frozenset(), token_id=1, allow_introspection=allowed),
        )
        assert view.get_middleware(request)[0] is configured


QUERY = "{ invoices { totalCount } }"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Freeze the clock of the rate limiter, at the start of a minute.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        A one item list holding the current time, which a test may change.
    """
    now = [1_800_000_000.0 - 1_800_000_000.0 % 60]
    monkeypatch.setattr("api.ratelimit.time", lambda: now[0])
    return now


def test_the_cache_is_redis() -> None:
    """The rate limit counters live in Redis, shared by every web process."""
    from django.conf import settings

    assert settings.CACHES["default"]["BACKEND"] == "django.core.cache.backends.redis.RedisCache"


def test_a_token_over_its_limit_gets_429_with_retry_after(
    settings: Any, clock: list[float]
) -> None:
    """The request after the limit is refused with JSON and the seconds until the window ends.

    Args:
        settings: Pytest-django settings fixture.
        clock: Frozen clock of the limiter.
    """
    settings.GRAPHQL_RATE_LIMIT_PER_MINUTE = 3
    _, token = make_world("A", ["invoices:read"])
    clock[0] += 45
    assert [post(QUERY, token).status_code for _ in range(3)] == [200, 200, 200]
    response = post(QUERY, token)
    assert response.status_code == 429
    assert response["Retry-After"] == "15"
    assert response["Content-Type"] == "application/json"
    assert "detail" in response.json()


def test_the_refusal_comes_before_the_query_runs(settings: Any, clock: list[float]) -> None:
    """A malformed document is answered 429, so nothing was parsed or executed.

    Args:
        settings: Pytest-django settings fixture.
        clock: Frozen clock of the limiter.
    """
    settings.GRAPHQL_RATE_LIMIT_PER_MINUTE = 1
    _, token = make_world("A", ["invoices:read"])
    post(QUERY, token)
    assert post("{ not valid", token).status_code == 429


def test_the_counter_starts_over_in_the_next_window(settings: Any, clock: list[float]) -> None:
    """A minute later the same token may call again.

    Args:
        settings: Pytest-django settings fixture.
        clock: Frozen clock of the limiter.
    """
    settings.GRAPHQL_RATE_LIMIT_PER_MINUTE = 1
    _, token = make_world("A", ["invoices:read"])
    assert post(QUERY, token).status_code == 200
    assert post(QUERY, token).status_code == 429
    clock[0] += 60
    assert post(QUERY, token).status_code == 200


def test_tokens_are_counted_apart(settings: Any, clock: list[float]) -> None:
    """One token running out does not slow another, even in the same tenant.

    Args:
        settings: Pytest-django settings fixture.
        clock: Frozen clock of the limiter.
    """
    settings.GRAPHQL_RATE_LIMIT_PER_MINUTE = 1
    tenant, first = make_world("A", ["invoices:read"])
    second = dev_token(tenant)
    assert post(QUERY, first).status_code == 200
    assert post(QUERY, first).status_code == 429
    assert post(QUERY, second).status_code == 200


def test_requests_without_a_token_are_401_and_not_counted(
    settings: Any, clock: list[float]
) -> None:
    """The 401 stays, and anonymous traffic never reaches a counter.

    Args:
        settings: Pytest-django settings fixture.
        clock: Frozen clock of the limiter.
    """
    settings.GRAPHQL_RATE_LIMIT_PER_MINUTE = 1
    assert [post(QUERY).status_code for _ in range(3)] == [401, 401, 401]
    redis_client = cache._cache.get_client()  # type: ignore[attr-defined]
    assert not redis_client.keys("*graphql-rate*")


def test_a_cache_outage_lets_the_request_through_with_a_warning(
    settings: Any,
    clock: list[float],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the cache is down the limiter fails open, logs the error type and never the token.

    Args:
        settings: Pytest-django settings fixture.
        clock: Frozen clock of the limiter.
        monkeypatch: Pytest monkeypatch fixture.
        caplog: Pytest log capture fixture.
    """

    def down(*args: object, **kwargs: object) -> None:
        raise ConnectionError("redis is down")

    settings.GRAPHQL_RATE_LIMIT_PER_MINUTE = 1
    _, token = make_world("A", ["invoices:read"])
    monkeypatch.setattr(cache, "add", down)
    with caplog.at_level("WARNING", logger="api.ratelimit"):
        assert [post(QUERY, token).status_code for _ in range(2)] == [200, 200]
    assert "ConnectionError" in caplog.text
    assert token not in caplog.text


def test_a_key_evicted_between_add_and_incr_counts_as_a_first_request(
    settings: Any, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An evicted counter restarts the window instead of failing the request.

    Args:
        settings: Pytest-django settings fixture.
        clock: Frozen clock of the limiter.
        monkeypatch: Pytest monkeypatch fixture.
    """

    def evicted(key: str, delta: int = 1) -> int:
        raise ValueError(f"Key '{key}' not found")

    settings.GRAPHQL_RATE_LIMIT_PER_MINUTE = 1
    _, token = make_world("A", ["invoices:read"])
    monkeypatch.setattr(cache, "incr", evicted)
    assert post(QUERY, token).status_code == 200


def test_retry_after_is_rounded_up(settings: Any, clock: list[float]) -> None:
    """A fraction of a second left in the window is reported as a whole second.

    Args:
        settings: Pytest-django settings fixture.
        clock: Frozen clock of the limiter.
    """
    settings.GRAPHQL_RATE_LIMIT_PER_MINUTE = 1
    _, token = make_world("A", ["invoices:read"])
    clock[0] += 10.4
    post(QUERY, token)
    assert post(QUERY, token)["Retry-After"] == "50"


def test_the_cache_guard_refuses_the_broker_database(settings: Any) -> None:
    """Clearing the cache must never flush the Celery broker database.

    Args:
        settings: Pytest-django settings fixture.
    """
    from tests.conftest import assert_cache_is_not_the_broker

    assert_cache_is_not_the_broker()
    settings.CACHES = {
        "default": {**settings.CACHES["default"], "LOCATION": settings.CELERY_BROKER_URL}
    }
    with pytest.raises(pytest.fail.Exception, match="broker"):
        assert_cache_is_not_the_broker()


def introspect(query: str, token: str, method: str) -> Any:
    """Send an introspection query by POST or GET.

    Args:
        query: GraphQL document.
        token: Plaintext API token.
        method: Either post or get.

    Returns:
        The decoded JSON body.
    """
    from django.test import Client

    from tests.test_api_graphql import URL

    if method == "post":
        return post(query, token).json()
    response = Client().get(URL, {"query": query}, headers={"Authorization": f"Bearer {token}"})
    return response.json()


INTROSPECTION_FORMS = [
    pytest.param(INTROSPECTION, "post", id="schema"),
    pytest.param('{ __type(name: "Query") { name } }', "post", id="type"),
    pytest.param("{ s: __schema { queryType { name } } }", "post", id="aliased"),
    pytest.param(
        "{ ...F } fragment F on Query { __schema { queryType { name } } }", "post", id="fragment"
    ),
    pytest.param(INTROSPECTION, "get", id="get"),
]


@pytest.mark.parametrize(("query", "method"), INTROSPECTION_FORMS)
def test_every_introspection_form_is_refused_for_an_ordinary_token(query: str, method: str) -> None:
    """Field, type, alias, fragment and GET forms are all refused outside DEBUG.

    Args:
        query: Introspection document.
        method: HTTP method used to send it.
    """
    _, token = make_world("A", ["invoices:read"])
    body = introspect(query, token, method)
    assert body["errors"][0]["extensions"]["code"] == "INTROSPECTION_DISABLED"


@pytest.mark.parametrize(("query", "method"), INTROSPECTION_FORMS)
def test_every_introspection_form_is_allowed_for_a_development_token(
    query: str, method: str
) -> None:
    """The same documents succeed for a development token.

    Args:
        query: Introspection document.
        method: HTTP method used to send it.
    """
    tenant, _ = make_world("A", ["invoices:read"])
    body = introspect(query, dev_token(tenant), method)
    assert "errors" not in body
    assert body["data"]
