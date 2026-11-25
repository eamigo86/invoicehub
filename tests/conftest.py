"""Shared pytest configuration: Hypothesis profiles, a temporary media root and no broker."""

import os
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from hypothesis import settings

from accounts.models import User
from billing.models import Customer
from reconciliation.models import BankAccount
from tenancy.context import tenant_context
from tenancy.models import Tenant
from tenancy.permissions import Permission, Principal
from tests.test_payments import make_tenant
from tests.test_reconciliation_models import make_account

# WeasyPrint falls back to ctypes.util.find_library, which reads this variable at call time.
# uv strips DYLD_* variables when it starts Python on macOS, so Homebrew's Pango is set here.
if sys.platform == "darwin":
    os.environ.setdefault("DYLD_FALLBACK_LIBRARY_PATH", "/opt/homebrew/lib")

settings.register_profile("default", deadline=None)
settings.register_profile("ci", derandomize=True, deadline=None)
settings.load_profile("ci" if os.environ.get("CI") else "default")


@pytest.fixture(autouse=True)
def media_root(settings: Any, tmp_path: Path) -> Path:
    """Point the file storage at a temporary directory, so tests never write into the project.

    Args:
        settings: Pytest-django settings fixture.
        tmp_path: Temporary directory of the test.

    Returns:
        The directory used as MEDIA_ROOT.
    """
    settings.MEDIA_ROOT = tmp_path / "media"
    return Path(settings.MEDIA_ROOT)


@pytest.fixture(autouse=True)
def enqueued(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the delay of the PDF task, so tests never reach a broker.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The mock standing in for delay.
    """
    from billing.tasks import render_invoice_pdf

    delay = MagicMock()
    monkeypatch.setattr(render_invoice_pdf, "delay", delay)
    return delay


@pytest.fixture(autouse=True)
def enqueued_email(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the delay of the invoice email task, so tests never reach a broker.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The mock standing in for delay.
    """
    from billing.tasks import send_invoice_email

    delay = MagicMock()
    monkeypatch.setattr(send_invoice_email, "delay", delay)
    return delay


@pytest.fixture(autouse=True)
def enqueued_import(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the delay of the statement import task, so tests never reach a broker.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The mock standing in for delay.
    """
    from reconciliation.tasks import import_statement

    delay = MagicMock()
    monkeypatch.setattr(import_statement, "delay", delay)
    return delay


@pytest.fixture(autouse=True)
def enqueued_reconcile(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the delay of the reconciliation run task, so tests never reach a broker.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The mock standing in for delay.
    """
    from reconciliation.tasks import reconcile_run

    delay = MagicMock()
    monkeypatch.setattr(reconcile_run, "delay", delay)
    return delay


@pytest.fixture
def tenant() -> Generator[Tenant]:
    """Create a tenant with issuer details and make it the active one.

    Yields:
        The tenant.
    """
    tenant = make_tenant()
    with tenant_context(tenant.pk):
        yield tenant


@pytest.fixture
def user() -> User:
    """Create the user behind the writer principal.

    Returns:
        The user.
    """
    return User.objects.create_user("ann", "ann@example.com", "pw")


@pytest.fixture
def writer(tenant: Tenant, user: User) -> Principal:
    """Build a principal that may write invoices and reconciliation data.

    Args:
        tenant: Active tenant.
        user: User the principal stands for.

    Returns:
        The principal.
    """
    permissions = frozenset(
        {Permission.INVOICES_READ, Permission.INVOICES_WRITE, Permission.RECONCILIATION_WRITE}
    )
    return Principal(tenant.pk, permissions, user_id=user.pk)


@pytest.fixture
def customer(tenant: Tenant) -> Customer:
    """Create a customer in the active tenant.

    Args:
        tenant: Active tenant.

    Returns:
        The customer.
    """
    created: Customer = Customer.objects.create(name="Bolt")
    return created


@pytest.fixture
def account(tenant: Tenant) -> BankAccount:
    """Create a bank account in the active tenant.

    Args:
        tenant: Active tenant.

    Returns:
        The account.
    """
    return make_account()
