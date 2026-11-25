"""The seed_demo command: guards, the worker round trip, idempotence and failures."""

from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from accounts.models import User
from reconciliation.models import BankStatementImport, ReconciliationRun
from reconciliation.parsing import StatementError
from reconciliation.seed import DEMO_EMAIL, DEMO_PASSWORD
from reconciliation.tasks import import_statement, reconcile_run
from tenancy.context import tenant_context
from tenancy.models import Tenant

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def worker(settings: Any, enqueued_import: MagicMock, enqueued_reconcile: MagicMock) -> None:
    """Turn DEBUG on and let the enqueued tasks run inline, standing in for the Celery worker.

    Args:
        settings: Pytest-django settings fixture.
        enqueued_import: Mock replacing the delay of the import task.
        enqueued_reconcile: Mock replacing the delay of the run task.
    """
    settings.DEBUG = True
    enqueued_import.side_effect = import_statement
    enqueued_reconcile.side_effect = reconcile_run


def run_command(*args: str) -> str:
    """Run seed_demo and capture what it prints.

    Args:
        *args: Command line options.

    Returns:
        The standard output of the command.
    """
    out = StringIO()
    call_command("seed_demo", *args, stdout=out)
    return out.getvalue()


def test_refuses_to_run_without_debug(settings: Any) -> None:
    """The demo password is published, so without DEBUG nothing is written.

    Args:
        settings: Pytest-django settings fixture.
    """
    settings.DEBUG = False
    with pytest.raises(CommandError, match="DEBUG"):
        run_command()
    assert not User.objects.exists()


@pytest.mark.usefixtures("worker")
def test_seeds_imports_and_reconciles_the_demo(media_root: Path) -> None:
    """With the worker running, the command prints the login and the reconciliation outcome.

    Args:
        media_root: Temporary MEDIA_ROOT of the test.
    """
    output = run_command()
    for expected in (
        "http://localhost:8010/admin/",
        DEMO_EMAIL,
        DEMO_PASSWORD,
        "4 applied",
        "1 suggested",
        "1 ambiguous",
        "2 bank rows unmatched",
    ):
        assert expected in output
    saved = media_root / "demo" / "bank_statement.csv"
    tenant = Tenant.objects.get()
    with tenant_context(tenant.pk):
        statement = BankStatementImport.objects.get()
        assert saved.read_bytes() == statement.file.read()
        assert ReconciliationRun.objects.get().status == ReconciliationRun.Status.COMPLETED


@pytest.mark.usefixtures("worker")
def test_a_second_run_changes_nothing() -> None:
    """Running again prints the same outcome and creates no second tenant, user or run."""
    first = run_command()
    second = run_command()
    assert second == first
    assert "4 applied" in second
    assert Tenant.objects.count() == 1
    assert User.objects.count() == 1
    with tenant_context(Tenant.objects.get().pk):
        assert ReconciliationRun.objects.count() == 1


def test_a_rerun_finishes_an_interrupted_demo(
    settings: Any, enqueued_import: MagicMock, enqueued_reconcile: MagicMock
) -> None:
    """After a timeout, a rerun imports and reconciles the same demo instead of skipping it.

    Args:
        settings: Pytest-django settings fixture.
        enqueued_import: Mock replacing the delay of the import task.
        enqueued_reconcile: Mock replacing the delay of the run task.
    """
    settings.DEBUG = True
    with pytest.raises(CommandError, match="docker compose down -v"):
        run_command("--timeout", "0")
    import_statement(*enqueued_import.call_args.args)
    enqueued_import.side_effect = import_statement
    enqueued_reconcile.side_effect = reconcile_run
    output = run_command()
    assert "4 applied" in output
    assert "1 ambiguous" in output
    assert Tenant.objects.count() == 1
    with tenant_context(Tenant.objects.get().pk):
        assert ReconciliationRun.objects.count() == 1


def test_it_polls_until_the_worker_finishes(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    enqueued_import: MagicMock,
    enqueued_reconcile: MagicMock,
) -> None:
    """While the import is pending the command sleeps and reads again.

    Args:
        settings: Pytest-django settings fixture.
        monkeypatch: Pytest monkeypatch fixture.
        enqueued_import: Mock replacing the delay of the import task.
        enqueued_reconcile: Mock replacing the delay of the run task.
    """
    settings.DEBUG = True
    enqueued_reconcile.side_effect = reconcile_run

    def worker_finishes(seconds: float) -> None:
        for call in enqueued_import.call_args_list:
            import_statement(*call.args)

    monkeypatch.setattr("reconciliation.management.commands.seed_demo.time.sleep", worker_finishes)
    assert "4 applied" in run_command()


def test_a_worker_that_never_runs_times_out(settings: Any) -> None:
    """Without a worker the command fails pointing at its logs, and the seed stays.

    Args:
        settings: Pytest-django settings fixture.
    """
    settings.DEBUG = True
    with pytest.raises(CommandError, match="worker.*down -v"):
        run_command("--timeout", "0")
    assert User.objects.filter(email=DEMO_EMAIL).exists()


@pytest.mark.usefixtures("worker")
def test_a_failed_import_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """The error of a failed import reaches the user.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """

    def broken(*args: object) -> None:
        raise StatementError("bad file")

    monkeypatch.setattr("reconciliation.tasks.parse_statement", broken)
    with pytest.raises(CommandError, match="bad file"):
        run_command()


@pytest.mark.usefixtures("worker")
def test_a_failed_run_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """The error of a failed reconciliation run reaches the user.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """

    def broken(*args: object) -> None:
        raise ValueError("engine bug")

    monkeypatch.setattr("reconciliation.engine.match", broken)
    with pytest.raises(CommandError, match="engine bug"):
        run_command()
