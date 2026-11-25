# Shop integration example

`shop.py` simulates an online store. When an order is closed it calls the InvoiceHub GraphQL API
to:

1. find the customer by tax id (`customers` query) and create it with `saveCustomer` when missing;
2. create a draft invoice with `saveDraft`;
3. issue it with `issueInvoice`, then print the invoice reference and total.

It uses only the Python standard library. Typed errors in mutation payloads, HTTP 401 and 429
(with `Retry-After`) and GraphQL errors end with a one-line message on stderr and exit status 1.

## Run it against the demo

```bash
make demo
TOKEN=$(docker compose exec -T web python manage.py issue_api_token \
  --user demo@example.com --tenant "Demo Company" \
  --scope invoices:read --scope invoices:write --name shop)
INVOICEHUB_API_TOKEN="$TOKEN" python3 examples/shop_integration/shop.py
```

The endpoint defaults to `http://localhost:8010/graphql/`; change it with `--url` or
`INVOICEHUB_GRAPHQL_URL`. Prefer the `INVOICEHUB_API_TOKEN` variable to `--token`, which shows up
in process listings.

The order is fixed. Running the script again finds the existing customer, so it creates no
duplicate, but it drafts and issues a second invoice for a second order.

## Token scopes

The token needs both `invoices:read` and `invoices:write`. Looking up the customer and the ids
that mutations return goes through the read services, so a token that can only write fails.
Without `invoices:write` the script prints the `PERMISSION_DENIED` error and exits with status 1.

`issue_api_token` builds the token from the permissions of the member you name, so it refuses
members who may not manage tokens, and it prints the plaintext token once, to stdout.
