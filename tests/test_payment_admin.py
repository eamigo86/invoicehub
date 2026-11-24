"""The payment admin: read-only, with a read-only allocations inline."""

import pytest
from django.contrib import admin
from django.test import RequestFactory

from accounts.models import User
from billing.admin import PaymentAdmin, PaymentAllocationInline
from billing.models import Payment, PaymentAllocation


def test_the_admin_allows_no_write_to_anyone() -> None:
    """Add, change and delete of payments are refused even to a superuser."""
    request = RequestFactory().get("/")
    request.user = User(is_active=True, is_superuser=True)
    model_admin = PaymentAdmin(Payment, admin.site)
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request)
    assert not model_admin.has_delete_permission(request)


def test_the_allocations_inline_is_read_only() -> None:
    """Allocations are listed on the payment page but cannot be added, changed or deleted."""
    request = RequestFactory().get("/")
    request.user = User(is_active=True, is_superuser=True)
    inline = PaymentAllocationInline(Payment, admin.site)
    assert PaymentAdmin.inlines == [PaymentAllocationInline]
    assert inline.model is PaymentAllocation
    assert not inline.has_add_permission(request)
    assert not inline.has_change_permission(request)
    assert not inline.has_delete_permission(request)


@pytest.mark.django_db
def test_the_models_are_described_by_amount_and_invoice() -> None:
    """The string forms name the amount, date and invoice."""
    payment = Payment(amount=1000, currency="USD", value_date="2026-06-15")
    assert str(payment) == "1000 USD on 2026-06-15"
    assert str(PaymentAllocation(amount=600)).startswith("600 on invoice")
