"""The shop example script, run end to end against a live server of the real app."""

import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from pytest_django.live_server_helper import LiveServer

from accounts.models import User
from billing.models import Customer, Invoice
from examples.shop_integration import shop
from tenancy.context import tenant_context
from tenancy.models import Membership, Tenant
from tenancy.permissions import Principal
from tenancy.tokens import issue_token
from tests.test_payments import make_tenant

pytestmark = pytest.mark.django_db(transaction=True)

ORDER_TOTAL = "85.00 USD"
"""Total of the fixed order: 2 x 24.50 plus 3 x 12.00, with no tax rate on the tenant."""


def make_tenant_token(scopes: list[str]) -> tuple[Tenant, str]:
    """Create a tenant with complete issuer details, an owner and an API token.

    Args:
        scopes: Scopes granted to the token.

    Returns:
        The tenant and the plaintext token.
    """
    tenant = make_tenant()
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    membership = Membership.objects.create(user=user, tenant=tenant, role=Membership.Role.OWNER)
    return tenant, issue_token(Principal.from_membership(membership), "shop", scopes)[1]


def run_shop(live_server: LiveServer, token: str, *extra: str) -> int:
    """Run the script against the live server.

    Args:
        live_server: Live server fixture of pytest-django.
        token: Plaintext API token.
        *extra: More command line options.

    Returns:
        The exit status of the script.
    """
    return shop.main(["--url", f"{live_server.url}/graphql/", "--token", token, *extra])


def test_a_closed_order_creates_the_customer_and_issues_the_invoice(
    live_server: LiveServer, capsys: pytest.CaptureFixture[str]
) -> None:
    """A new customer is created, the draft is issued and its number and total are printed.

    Args:
        live_server: Live server fixture of pytest-django.
        capsys: Pytest output capture.
    """
    tenant, token = make_tenant_token(["invoices:read", "invoices:write"])
    assert run_shop(live_server, token) == 0
    with tenant_context(tenant.pk):
        customer = Customer.objects.get(tax_id=shop.ORDER.tax_id)
        invoice = Invoice.objects.get()
        assert invoice.customer == customer
        assert invoice.status == Invoice.Status.ISSUED
        assert invoice.lines.count() == 2
    out = capsys.readouterr().out
    assert f"Issued invoice {invoice.reference}" in out
    assert f"total {ORDER_TOTAL}" in out
    assert re.search(r"INV-\d{4}-\d{5}", out)


def test_an_existing_customer_is_reused_and_a_rerun_issues_a_second_invoice(
    live_server: LiveServer,
) -> None:
    """The customer is found by tax id, so two orders make two invoices and one customer.

    Args:
        live_server: Live server fixture of pytest-django.
    """
    tenant, token = make_tenant_token(["invoices:read", "invoices:write"])
    with tenant_context(tenant.pk):
        Customer.objects.create(name="Already here", tax_id=shop.ORDER.tax_id)
    assert run_shop(live_server, token) == 0
    assert run_shop(live_server, token) == 0
    with tenant_context(tenant.pk):
        assert Customer.objects.count() == 1
        assert Invoice.objects.filter(customer__name="Already here").count() == 2
        assert Invoice.objects.filter(status=Invoice.Status.ISSUED).count() == 2


def test_a_token_without_write_scope_gets_a_typed_error_and_a_failing_exit(
    live_server: LiveServer, capsys: pytest.CaptureFixture[str]
) -> None:
    """The typed error of the payload is printed with its code, message and field.

    Args:
        live_server: Live server fixture of pytest-django.
        capsys: Pytest output capture.
    """
    tenant, token = make_tenant_token(["invoices:read"])
    assert run_shop(live_server, token) == 1
    err = capsys.readouterr().err
    assert "PERMISSION_DENIED" in err
    with tenant_context(tenant.pk):
        assert Customer.objects.count() == 0


def test_a_rejected_token_is_reported_as_401(
    live_server: LiveServer, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unknown token ends with a short message instead of a traceback.

    Args:
        live_server: Live server fixture of pytest-django.
        capsys: Pytest output capture.
    """
    assert run_shop(live_server, "ih_unknown") == 1
    assert "401" in capsys.readouterr().err


def test_a_rate_limited_token_is_told_when_to_retry(
    live_server: LiveServer, settings: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 429 is reported with the seconds from the Retry-After header.

    Args:
        live_server: Live server fixture of pytest-django.
        settings: Pytest-django settings fixture.
        capsys: Pytest output capture.
    """
    settings.GRAPHQL_RATE_LIMIT_PER_MINUTE = 1
    _, token = make_tenant_token(["invoices:read", "invoices:write"])
    assert run_shop(live_server, token) == 1
    assert re.search(r"429.*retry in \d+ seconds", capsys.readouterr().err)


def test_graphql_errors_are_raised_with_their_messages(live_server: LiveServer) -> None:
    """A document the schema refuses raises an error that carries the server message.

    Args:
        live_server: Live server fixture of pytest-django.
    """
    _, token = make_tenant_token(["invoices:read"])
    with pytest.raises(shop.ShopError, match="nope"):
        shop.graphql(f"{live_server.url}/graphql/", token, "{ nope }", {})


def test_an_unreachable_server_is_reported_without_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A refused connection ends with a short message.

    Args:
        capsys: Pytest output capture.
    """
    assert shop.main(["--url", "http://127.0.0.1:9/graphql/", "--token", "ih_x"]) == 1
    assert "Cannot reach" in capsys.readouterr().err


def test_the_url_and_token_can_come_from_the_environment(
    live_server: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented variables replace the options, so no secret has to be on the command line.

    Args:
        live_server: Live server fixture of pytest-django.
        monkeypatch: Pytest monkeypatch fixture.
    """
    _, token = make_tenant_token(["invoices:read", "invoices:write"])
    monkeypatch.setenv("INVOICEHUB_API_TOKEN", token)
    monkeypatch.setenv("INVOICEHUB_GRAPHQL_URL", f"{live_server.url}/graphql/")
    assert shop.main([]) == 0


def test_a_missing_token_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an option or a variable the script stops with a usage error.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.delenv("INVOICEHUB_API_TOKEN", raising=False)
    with pytest.raises(SystemExit) as stop:
        shop.main([])
    assert stop.value.code == 2


@contextmanager
def serve(status: int, body: bytes, delay: float = 0) -> Iterator[str]:
    """Serve one canned answer to every POST on a local port.

    Args:
        status: HTTP status of the answer.
        body: Body of the answer.
        delay: Seconds to wait before answering.

    Yields:
        The GraphQL endpoint of the server, which stops when the generator is closed.
    """

    class Handler(BaseHTTPRequestHandler):
        """Answer every POST with the canned response."""

        def do_POST(self) -> None:  # noqa: N802 - the name http.server looks for
            """Read the request and send the canned response."""
            self.rfile.read(int(self.headers["Content-Length"]))
            time.sleep(delay)
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            """Keep the test output quiet.

            Args:
                format: Log format, unused.
                *args: Log arguments, unused.
            """

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/graphql/"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (500, b"<html>boom</html>", "HTTP 500"),
        (200, b"<html>not json</html>", "not JSON"),
        (200, b'{"data": null}', "no data"),
        (200, b'{"errors": [{}]}', "GraphQL error"),
        (400, b'{"errors": [{}]}', "GraphQL error"),
        (200, b"[1]", "not a GraphQL response"),
    ],
)
def test_a_broken_answer_is_reported_in_one_line_without_the_token(
    status: int, body: bytes, expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A server error, a body that is not a GraphQL response or an error without a message.

    Args:
        status: HTTP status of the canned answer.
        body: Body of the canned answer.
        expected: Text the error line must contain.
        capsys: Pytest output capture.
    """
    with serve(status, body) as url:
        assert shop.main(["--url", url, "--token", "ih_secret"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: ")
    assert expected in err
    assert err.count("\n") == 1
    assert "ih_secret" not in err


def test_a_read_timeout_is_reported_in_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A server that accepts the request and never answers ends with a short message.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        capsys: Pytest output capture.
    """
    monkeypatch.setattr(shop, "TIMEOUT_SECONDS", 0.2)
    with serve(200, b"{}", delay=1) as url:
        assert shop.main(["--url", url, "--token", "ih_secret"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: ")
    assert "Timed out" in err
    assert "ih_secret" not in err
