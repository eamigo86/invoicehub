"""TenantModel: tenant-scoped manager and application-level save guard.

The models here are abstract subclasses built in an isolated registry and never touch the
database, so these tests only cover the application layer. Customer tests cover the database.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from django.db import models
from django.test.utils import isolate_apps

from tenancy.context import TenantMismatch, TenantNotSet, current_tenant_id
from tenancy.models import TenantModel

A = uuid.uuid4()
B = uuid.uuid4()


@contextmanager
def active(tenant: uuid.UUID) -> Iterator[None]:
    """Activate a tenant in the context variable only, without opening a transaction.

    Args:
        tenant: Tenant to activate.

    Yields:
        Nothing; the tenant is active while the block runs.
    """
    token = current_tenant_id.set(tenant)
    try:
        yield
    finally:
        current_tenant_id.reset(token)


@pytest.fixture
def widget_model() -> Iterator[type[TenantModel]]:
    """Build a concrete tenant model in an isolated app registry.

    Yields:
        A throwaway concrete subclass of TenantModel.
    """
    with isolate_apps("tenancy"):

        class Widget(TenantModel):
            """Throwaway model."""

            class Meta:
                """Model options."""

                app_label = "tenancy"

        yield Widget


@pytest.fixture
def no_db_save(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the database write of Model.save with a no-op.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """

    def noop(self: models.Model, *args: Any, **kwargs: Any) -> None:
        """Skip the database write.

        Args:
            self: Instance being saved.
            *args: Positional arguments, ignored.
            **kwargs: Keyword arguments, ignored.
        """

    monkeypatch.setattr(models.Model, "save", noop)


def test_manager_requires_a_tenant(widget_model: type[TenantModel]) -> None:
    """Without an active tenant the default manager refuses to build a queryset.

    Args:
        widget_model: Throwaway tenant model.
    """
    with pytest.raises(TenantNotSet):
        widget_model.objects.all()


def test_manager_filters_by_the_current_tenant(widget_model: type[TenantModel]) -> None:
    """The queryset is restricted to the active tenant.

    Args:
        widget_model: Throwaway tenant model.
    """
    with active(A):
        sql, params = widget_model.objects.all().query.sql_with_params()
    assert '"tenant_id" = %s' in sql
    assert A in params or str(A) in params


def test_save_assigns_the_current_tenant(widget_model: type[TenantModel], no_db_save: None) -> None:
    """A new row without a tenant receives the active one.

    Args:
        widget_model: Throwaway tenant model.
        no_db_save: Fixture disabling the database write.
    """
    widget = widget_model()
    with active(A):
        widget.save()
    assert widget.tenant_id == A


def test_save_without_a_tenant_raises(widget_model: type[TenantModel], no_db_save: None) -> None:
    """Saving with no active tenant raises TenantNotSet.

    Args:
        widget_model: Throwaway tenant model.
        no_db_save: Fixture disabling the database write.
    """
    with pytest.raises(TenantNotSet):
        widget_model().save()


def test_save_of_another_tenants_row_raises(
    widget_model: type[TenantModel], no_db_save: None
) -> None:
    """An instance owned by tenant B cannot be saved while tenant A is active.

    Args:
        widget_model: Throwaway tenant model.
        no_db_save: Fixture disabling the database write.
    """
    widget = widget_model(tenant_id=B)
    with pytest.raises(TenantMismatch), active(A):
        widget.save()
