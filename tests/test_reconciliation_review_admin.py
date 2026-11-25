"""Reconciliation admins: runs with their metrics and the proposal review queue.

These tests use real transactions because the tenant reaches PostgreSQL only when a request's
outermost transaction begins.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest
from django.contrib import admin
from django.test import Client

from billing.models import Customer, Invoice
from reconciliation.models import BankAccount, MatchProposal, ReconciliationRun
from tenancy.context import tenant_context
from tenancy.models import Membership, Tenant
from tenancy.permissions import Permission, Principal
from tests.test_payments import issued_invoice, make_tenant
from tests.test_reconciliation_admin import login
from tests.test_reconciliation_apply import propose
from tests.test_reconciliation_engine import make_tx
from tests.test_reconciliation_models import make_account

pytestmark = pytest.mark.django_db(transaction=True)

BASE = "/admin/reconciliation"
PENDING = MatchProposal.State.PENDING


@pytest.fixture
def tenant() -> Tenant:
    """Create a tenant that is not active outside requests.

    Returns:
        The tenant, with complete issuer details.
    """
    return make_tenant()


def seed_invoice(tenant: Tenant, cents: int = 1000) -> Invoice:
    """Issue an invoice in the tenant.

    Args:
        tenant: Tenant that owns the invoice.
        cents: Total of the invoice.

    Returns:
        The issued invoice.
    """
    with tenant_context(tenant.pk):
        customer, _ = Customer.objects.get_or_create(name="Bolt")
        return issued_invoice(Principal(tenant.pk, frozenset(Permission)), customer, cents)


def seed(tenant: Tenant, tag: str = "a", status: str = "suggested") -> MatchProposal:
    """Create a pending one to one proposal of an invoice and a bank transaction of 1000 cents.

    Args:
        tenant: Tenant that owns the rows.
        tag: Character that makes the transaction hash unique.
        status: Engine status of the proposal.

    Returns:
        The proposal.
    """
    invoice = seed_invoice(tenant)
    with tenant_context(tenant.pk):
        account = BankAccount.objects.first() or make_account()
        return propose("1:1", [make_tx(account, 1000, tag=tag)], [invoice], status=status)


def state_of(tenant: Tenant, proposal: MatchProposal) -> str:
    """Read the stored state of a proposal.

    Args:
        tenant: Tenant that owns the proposal.
        proposal: Proposal to read again.

    Returns:
        Its state.
    """
    with tenant_context(tenant.pk):
        return str(MatchProposal.objects.get(pk=proposal.pk).state)


def act(client: Client, name: str, *proposals: MatchProposal) -> list[str]:
    """Run a changelist action on proposals and collect the messages it leaves.

    Args:
        client: Logged in test client.
        name: Action name.
        *proposals: Proposals to select.

    Returns:
        The messages shown on the page the action redirects to.
    """
    data = {"action": name, "_selected_action": [str(p.pk) for p in proposals]}
    response = client.post(f"{BASE}/matchproposal/", data, follow=True)
    assert response.status_code == 200
    return [str(m) for m in response.context["messages"]]


def test_starting_a_run_enqueues_once_and_a_second_start_lands_on_the_same_run(
    client: Client, tenant: Tenant, enqueued_reconcile: MagicMock
) -> None:
    """Both starts redirect to the one pending run, which is enqueued a single time.

    Args:
        client: Django test client fixture.
        tenant: Tenant that starts the runs.
        enqueued_reconcile: Mock standing in for the run task delay.
    """
    login(client, tenant)
    locations = [client.post(f"{BASE}/reconciliationrun/add/", {})["Location"] for _ in range(2)]
    with tenant_context(tenant.pk):
        run = ReconciliationRun.objects.get()
    assert locations == [f"{BASE}/reconciliationrun/{run.pk}/change/"] * 2
    assert run.status == ReconciliationRun.Status.PENDING
    enqueued_reconcile.assert_called_once_with(str(tenant.pk), str(run.pk))


def test_a_viewer_cannot_start_runs_or_use_the_actions(
    client: Client, tenant: Tenant, enqueued_reconcile: MagicMock
) -> None:
    """A viewer gets 403 on the run form, and a posted action changes nothing.

    Args:
        client: Django test client fixture.
        tenant: Tenant that owns the rows.
        enqueued_reconcile: Mock standing in for the run task delay.
    """
    proposal = seed(tenant)
    login(client, tenant, Membership.Role.VIEWER)
    url = f"{BASE}/reconciliationrun/add/"
    assert client.get(url).status_code == 403
    assert client.post(url, {}).status_code == 403
    client.post(
        f"{BASE}/matchproposal/",
        {"action": "reject_selected", "_selected_action": [str(proposal.pk)]},
    )
    assert state_of(tenant, proposal) == PENDING
    with tenant_context(tenant.pk):
        assert not ReconciliationRun.objects.filter(status="pending").exists()
    enqueued_reconcile.assert_not_called()


@pytest.mark.parametrize(
    ("part", "whole", "shown"),
    [(1, 3, "33.3%"), (2, 3, "66.7%"), (1, 8, "12.5%"), (5, 5, "100.0%"), (0, 4, "0.0%")],
)
def test_percentages_use_integer_math_with_one_decimal(part: int, whole: int, shown: str) -> None:
    """A share is rounded half up to one decimal without floats.

    Args:
        part: Numerator.
        whole: Denominator.
        shown: Expected display text.
    """
    from reconciliation.admin import format_percent

    assert format_percent(part, whole) == shown


def test_a_zero_denominator_shows_a_dash() -> None:
    """With nothing considered or decided there is no share to show."""
    from reconciliation.admin import format_percent

    assert format_percent(0, 0) == "-"


def test_run_metrics_are_shown_over_transactions_considered(client: Client, tenant: Tenant) -> None:
    """The run list shows the shares of auto, suggested and unmatched and the acceptance rate.

    Args:
        client: Django test client fixture.
        tenant: Tenant that owns the runs.
    """
    with tenant_context(tenant.pk):
        run = ReconciliationRun.objects.create(
            config={},
            status=ReconciliationRun.Status.COMPLETED,
            transactions_considered=16,
            auto_count=4,
            suggested_count=6,
            unmatched_transactions=2,
        )
        empty = ReconciliationRun.objects.create(config={}, status=ReconciliationRun.Status.FAILED)
        for engine_status, state in [
            ("suggested", "applied"),
            ("suggested", "applied"),
            ("suggested", "reassigned"),
            ("suggested", "rejected"),
            ("suggested", "pending"),
            ("suggested", "undone"),
            ("auto", "rejected"),
        ]:
            MatchProposal.objects.create(
                run=run,
                kind="1:1",
                engine_status=engine_status,
                state=state,
                score=0.5,
                breakdown={},
                residual=0,
            )
    login(client, tenant)
    response = client.get(f"{BASE}/reconciliationrun/")
    body = response.content.decode()
    for shown in ("25.0%", "37.5%", "12.5%", "75.0%"):
        assert shown in body
    model_admin: Any = admin.site._registry[ReconciliationRun]
    assert response.context_data is not None
    rows = {row.pk: row for row in response.context_data["cl"].result_list}
    assert model_admin.percent_auto(rows[empty.pk]) == "-"
    assert model_admin.suggestion_acceptance(rows[empty.pk]) == "-"
    assert model_admin.suggestion_acceptance(rows[run.pk]) == "75.0%"


def test_run_and_proposal_pages_render_for_an_owner(client: Client, tenant: Tenant) -> None:
    """The lists and details show the run config, error, breakdown and the links between them.

    Args:
        client: Django test client fixture.
        tenant: Tenant that owns the rows.
    """
    proposal = seed(tenant)
    with tenant_context(tenant.pk):
        MatchProposal.objects.filter(pk=proposal.pk).update(breakdown={"amount": 1.0, "name": 0.5})
        ReconciliationRun.objects.filter(pk=proposal.run_id).update(
            config={"auto_threshold": 0.9}, error="boom"
        )
    login(client, tenant)
    assert client.get(f"{BASE}/reconciliationrun/add/").status_code == 200
    run_page = client.get(f"{BASE}/reconciliationrun/{proposal.run_id}/change/").content.decode()
    assert "auto_threshold" in run_page and "boom" in run_page
    assert f"/matchproposal/?run__id__exact={proposal.run_id}" in run_page
    listing = client.get(f"{BASE}/matchproposal/").content.decode()
    assert "10.00 USD" in listing
    detail = client.get(f"{BASE}/matchproposal/{proposal.pk}/change/").content.decode()
    assert "amount: 1.00" in detail and "name: 0.50" in detail
    assert f"/matchproposal/{proposal.pk}/reassign/" in detail
    assert client.get(f"{BASE}/matchproposal/?run__id__exact={proposal.run_id}").status_code == 200


def test_runs_and_proposals_are_read_only(client: Client, tenant: Tenant) -> None:
    """Neither admin changes or deletes rows, and proposals cannot even be added.

    Args:
        client: Django test client fixture.
        tenant: Tenant that owns the rows.
    """
    proposal = seed(tenant)
    login(client, tenant)
    for model, pk in (("reconciliationrun", proposal.run_id), ("matchproposal", proposal.pk)):
        assert client.post(f"{BASE}/{model}/{pk}/change/", {}).status_code == 403
        assert client.post(f"{BASE}/{model}/{pk}/delete/", {"post": "yes"}).status_code == 403
    assert client.get(f"{BASE}/matchproposal/add/").status_code == 403
    assert state_of(tenant, proposal) == PENDING


def test_accept_reject_and_undo_actions_change_state_and_report_refusals(
    client: Client, tenant: Tenant
) -> None:
    """Each action decides its rows, and a refused row is a message instead of an error.

    Args:
        client: Django test client fixture.
        tenant: Tenant that owns the rows.
    """
    first, second, third = seed(tenant, "a"), seed(tenant, "b"), seed(tenant, "c")
    login(client, tenant)
    assert act(client, "accept_selected", first, second) == ["Accepted 2 proposals."]
    assert state_of(tenant, first) == state_of(tenant, second) == "applied"
    assert act(client, "reject_selected", third) == ["Rejected 1 proposal."]
    assert state_of(tenant, third) == "rejected"
    messages = act(client, "reject_selected", first, third)
    assert len(messages) == 2 and all(" is " in m or "cannot" in m for m in messages)
    assert state_of(tenant, first) == "applied"
    assert act(client, "undo_selected", first) == ["Undone 1 proposal."]
    assert state_of(tenant, first) == "undone"
    assert any("cannot be undone" in m for m in act(client, "undo_selected", first, third))
    assert any("cannot be applied" in m for m in act(client, "accept_selected", first))
    assert state_of(tenant, first) == "undone"


def test_reassign_form_pays_other_invoices_with_the_same_transaction(
    client: Client, tenant: Tenant
) -> None:
    """The form lists payable invoices, and the post reassigns and redirects to the detail.

    Args:
        client: Django test client fixture.
        tenant: Tenant that owns the rows.
    """
    proposal = seed(tenant)
    other = seed_invoice(tenant)
    login(client, tenant)
    url = f"{BASE}/matchproposal/{proposal.pk}/reassign/"
    page = client.get(url)
    assert page.status_code == 200
    assert other.reference in page.content.decode()
    response = client.post(url, {"invoices": [str(other.pk)]}, follow=True)
    assert response.redirect_chain == [(f"{BASE}/matchproposal/{proposal.pk}/change/", 302)]
    assert "Reassigned" in " ".join(str(m) for m in response.context["messages"])
    assert state_of(tenant, proposal) == "reassigned"
    with tenant_context(tenant.pk):
        assert Invoice.objects.get(pk=other.pk).status == Invoice.Status.PAID


def test_reassign_errors_rerender_the_form(client: Client, tenant: Tenant) -> None:
    """An empty choice and an invoice held by another proposal both keep the proposal pending.

    Args:
        client: Django test client fixture.
        tenant: Tenant that owns the rows.
    """
    proposal, rival = seed(tenant, "a"), seed(tenant, "b")
    with tenant_context(tenant.pk):
        held = rival.invoices.get()
    login(client, tenant)
    url = f"{BASE}/matchproposal/{proposal.pk}/reassign/"
    empty = client.post(url, {})
    assert empty.status_code == 200 and "required" in empty.content.decode()
    clash = client.post(url, {"invoices": [str(held.pk)]})
    assert clash.status_code == 200
    assert "already held" in " ".join(str(m) for m in clash.context["messages"])
    assert state_of(tenant, proposal) == PENDING


def test_a_decided_proposal_cannot_be_reassigned(client: Client, tenant: Tenant) -> None:
    """Both the form and the post turn a rejected proposal away with a message.

    Args:
        client: Django test client fixture.
        tenant: Tenant that owns the rows.
    """
    proposal = seed(tenant)
    other = seed_invoice(tenant)
    login(client, tenant)
    act(client, "reject_selected", proposal)
    url = f"{BASE}/matchproposal/{proposal.pk}/reassign/"
    for response in (
        client.get(url, follow=True),
        client.post(url, {"invoices": [str(other.pk)]}, follow=True),
    ):
        assert response.redirect_chain[-1] == (f"{BASE}/matchproposal/{proposal.pk}/change/", 302)
        assert "pending" in " ".join(str(m) for m in response.context["messages"])
    assert state_of(tenant, proposal) == "rejected"
    detail = client.get(f"{BASE}/matchproposal/{proposal.pk}/change/").content.decode()
    assert "/reassign/" not in detail


def test_a_viewer_cannot_open_or_post_the_reassign_form(client: Client, tenant: Tenant) -> None:
    """The form needs reconciliation:write like the actions do.

    Args:
        client: Django test client fixture.
        tenant: Tenant that owns the rows.
    """
    proposal = seed(tenant)
    other = seed_invoice(tenant)
    login(client, tenant, Membership.Role.VIEWER)
    url = f"{BASE}/matchproposal/{proposal.pk}/reassign/"
    assert client.get(url).status_code == 403
    assert client.post(url, {"invoices": [str(other.pk)]}).status_code == 403
    assert state_of(tenant, proposal) == PENDING
