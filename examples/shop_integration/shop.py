"""Example integration: an online store that invoices an order through the GraphQL API.

When an order is closed the script finds the customer by tax id, creates it when it does not
exist, creates a draft invoice from the order lines and issues it. It needs an API token with the
invoices:read and invoices:write scopes and uses only the standard library.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_URL = "http://localhost:8010/graphql/"
"""GraphQL endpoint of the Compose stack started by make demo."""
URL_VARIABLE = "INVOICEHUB_GRAPHQL_URL"
"""Environment variable that holds the endpoint when the option is omitted."""
TOKEN_VARIABLE = "INVOICEHUB_API_TOKEN"  # noqa: S105 - the name of a variable, not a secret
"""Environment variable that holds the API token when the option is omitted."""
TIMEOUT_SECONDS = 30
"""Seconds to wait for one request."""

FIND_CUSTOMER = """
query FindCustomer($filter: CustomerFilterInput) {
  customers(filter: $filter) { results(first: 1) { id name } }
}
"""
SAVE_CUSTOMER = """
mutation SaveCustomer($input: CustomerInput!) {
  saveCustomer(input: $input) { errors { code message field } customer { id name } }
}
"""
SAVE_DRAFT = """
mutation SaveDraft($input: DraftInvoiceInput!) {
  saveDraft(input: $input) { errors { code message field } invoice { id } }
}
"""
ISSUE_INVOICE = """
mutation IssueInvoice($id: ID!) {
  issueInvoice(id: $id) {
    errors { code message field }
    invoice { id reference total currency }
  }
}
"""


class ShopError(Exception):
    """A failure the script reports as one line and a failing exit status."""


@dataclass(frozen=True)
class OrderLine:
    """One line of a closed order.

    Attributes:
        description: What was sold.
        quantity: Units sold, as a decimal string with up to 3 decimals.
        unit_price: Price of one unit in cents; GraphQL carries BigInt values as strings.
    """

    description: str
    quantity: str
    unit_price: int


@dataclass(frozen=True)
class Order:
    """A closed order of the online store.

    Attributes:
        customer_name: Name of the buyer.
        tax_id: Tax id of the buyer, which identifies the customer.
        email: Email of the buyer, where invoices are sent.
        lines: Lines of the order.
    """

    customer_name: str
    tax_id: str
    email: str
    lines: tuple[OrderLine, ...]


ORDER = Order(
    customer_name="Blue Door Coffee LLC",
    tax_id="12-3456789",
    email="orders@bluedoor.example",
    lines=(
        OrderLine("Espresso beans, 1 kg", "2", 2450),
        OrderLine("Ceramic mug", "3", 1200),
    ),
)
"""The fixed order this example simulates."""


def graphql(url: str, token: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """Send one GraphQL request and return its data.

    Args:
        url: GraphQL endpoint.
        token: API token, sent as a bearer credential.
        query: GraphQL document.
        variables: Variables of the document.

    Returns:
        The data member of the response.

    Raises:
        ShopError: When the server cannot be reached, answers with an HTTP error such as 401 or
            429, or reports GraphQL errors.
    """
    request = urllib.request.Request(  # noqa: S310 - the endpoint is operator supplied
        url,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            body = json.load(response)
    except urllib.error.HTTPError as error:
        raise ShopError(_http_message(error)) from None
    except urllib.error.URLError as error:
        raise ShopError(f"Cannot reach {url}: {error.reason}") from None
    if body.get("errors"):
        raise ShopError("GraphQL error: " + "; ".join(e["message"] for e in body["errors"]))
    data: dict[str, Any] = body["data"]
    return data


def _http_message(error: urllib.error.HTTPError) -> str:
    """Describe an HTTP error in one line.

    Args:
        error: The error raised by urllib.

    Returns:
        A message that names the status, the seconds from Retry-After for 429, or the GraphQL
        errors a 400 carries.
    """
    if error.code == 401:
        return "HTTP 401: the API token was rejected."
    if error.code == 429:
        wait = error.headers.get("Retry-After", "?")
        return f"HTTP 429: rate limit exceeded, retry in {wait} seconds."
    try:
        errors = json.load(error)["errors"]
    except (ValueError, KeyError, TypeError):
        return f"HTTP {error.code}: {error.reason}"
    return "GraphQL error: " + "; ".join(e["message"] for e in errors)


def check(payload: dict[str, Any]) -> dict[str, Any]:
    """Raise the first typed error of a mutation payload.

    Args:
        payload: The payload of a mutation, with an errors list.

    Returns:
        The same payload, when it has no errors.

    Raises:
        ShopError: When the payload carries errors, naming the code, message and field.
    """
    if payload["errors"]:
        raise ShopError(
            "; ".join(
                f"{e['code']}: {e['message']}" + (f" (field {e['field']})" if e["field"] else "")
                for e in payload["errors"]
            )
        )
    return payload


def format_money(cents: int, currency: str) -> str:
    """Format integer cents without floats.

    Args:
        cents: Amount in cents.
        currency: Currency code.

    Returns:
        The amount with two decimals followed by the currency, for example 85.00 USD.
    """
    return f"{cents // 100}.{cents % 100:02d} {currency}"


def invoice_order(url: str, token: str, order: Order) -> str:
    """Invoice a closed order: find or create the customer, draft and issue the invoice.

    Args:
        url: GraphQL endpoint.
        token: API token with the invoices:read and invoices:write scopes.
        order: The closed order.

    Returns:
        A line with the issued invoice reference and total.
    """
    found = graphql(url, token, FIND_CUSTOMER, {"filter": {"taxId": {"exact": order.tax_id}}})
    results = found["customers"]["results"]
    if results:
        customer_id = results[0]["id"]
    else:
        customer = {"name": order.customer_name, "taxId": order.tax_id, "emails": [order.email]}
        saved = graphql(url, token, SAVE_CUSTOMER, {"input": customer})
        customer_id = check(saved["saveCustomer"])["customer"]["id"]
    lines = [
        {
            "description": line.description,
            "quantity": line.quantity,
            "unitPrice": str(line.unit_price),
        }
        for line in order.lines
    ]
    draft = {"customerId": customer_id, "notes": "Online shop order", "lines": lines}
    saved = graphql(url, token, SAVE_DRAFT, {"input": draft})
    draft_id = check(saved["saveDraft"])["invoice"]["id"]
    issued = graphql(url, token, ISSUE_INVOICE, {"id": draft_id})
    invoice = check(issued["issueInvoice"])["invoice"]
    total = format_money(int(invoice["total"]), invoice["currency"])
    return f"Issued invoice {invoice['reference']} for {order.customer_name}, total {total}"


def main(argv: list[str] | None = None) -> int:
    """Run the example: invoice the fixed order and report the result.

    Args:
        argv: Command line arguments, taken from the process when omitted.

    Returns:
        The exit status, 0 when the invoice was issued and 1 when the script reported an error.
    """
    parser = argparse.ArgumentParser(description="Invoice a closed shop order through GraphQL.")
    parser.add_argument("--url", default=os.environ.get(URL_VARIABLE, DEFAULT_URL))
    parser.add_argument(
        "--token",
        default=os.environ.get(TOKEN_VARIABLE),
        help=f"API token; prefer the {TOKEN_VARIABLE} variable, which stays out of ps output.",
    )
    args = parser.parse_args(argv)
    if not args.token:
        parser.error(f"pass --token or set {TOKEN_VARIABLE}")
    try:
        print(invoice_order(args.url, args.token, ORDER))
    except ShopError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
