"""System checks that keep unique constraints tenant-aware."""

from collections.abc import Iterator

import pytest
from django.core import checks
from django.db import models
from django.test.utils import isolate_apps

from tenancy.checks import check_tenant_models, tenant_models_check
from tenancy.models import TenantModel


@pytest.fixture
def isolated() -> Iterator[None]:
    """Run a test inside an isolated app registry for the tenancy label.

    Yields:
        Nothing; models defined during the test live in the isolated registry.
    """
    with isolate_apps("tenancy"):
        yield


def ids(model: type[models.Model]) -> list[str]:
    """Run the check on one model and list the ids of its messages.

    Args:
        model: Model class to check.

    Returns:
        The ids of the reported messages.
    """
    return [m.id for m in check_tenant_models([model])]  # type: ignore[misc]


def test_correct_model_is_silent(isolated: None) -> None:
    """Constraints that include the tenant, and a plain primary key, raise nothing.

    Args:
        isolated: Fixture providing an isolated registry.
    """

    class Good(TenantModel):
        """Model with tenant-aware uniqueness."""

        number = models.IntegerField()
        code = models.CharField(max_length=5)

        class Meta:
            """Model options."""

            app_label = "tenancy"
            constraints = [models.UniqueConstraint(fields=["tenant", "number"], name="g_u")]
            unique_together = [("tenant", "code")]

    assert ids(Good) == []


def test_unique_constraint_without_tenant_is_e001(isolated: None) -> None:
    """A UniqueConstraint that omits the tenant is tenancy.E001.

    Args:
        isolated: Fixture providing an isolated registry.
    """

    class Bad(TenantModel):
        """Model with a global unique constraint."""

        number = models.IntegerField()

        class Meta:
            """Model options."""

            app_label = "tenancy"
            constraints = [models.UniqueConstraint(fields=["number"], name="b_u")]

    assert ids(Bad) == ["tenancy.E001"]


def test_unique_together_without_tenant_is_e001(isolated: None) -> None:
    """A unique_together that omits the tenant is tenancy.E001.

    Args:
        isolated: Fixture providing an isolated registry.
    """

    class Bad(TenantModel):
        """Model with a global unique_together."""

        number = models.IntegerField()
        code = models.CharField(max_length=5)

        class Meta:
            """Model options."""

            app_label = "tenancy"
            unique_together = [("number", "code")]

    assert ids(Bad) == ["tenancy.E001"]


def test_unique_field_is_e002(isolated: None) -> None:
    """A non-primary-key field with unique=True is tenancy.E002.

    Args:
        isolated: Fixture providing an isolated registry.
    """

    class Bad(TenantModel):
        """Model with a globally unique field."""

        code = models.CharField(max_length=5, unique=True)

        class Meta:
            """Model options."""

            app_label = "tenancy"

    assert ids(Bad) == ["tenancy.E002"]


def test_abstract_and_plain_models_are_ignored(isolated: None) -> None:
    """Models that are not concrete tenant models are never reported.

    Args:
        isolated: Fixture providing an isolated registry.
    """

    class Plain(models.Model):
        """Ordinary model with a unique field."""

        code = models.CharField(max_length=5, unique=True)

        class Meta:
            """Model options."""

            app_label = "tenancy"

    assert ids(Plain) == []
    assert ids(TenantModel) == []


def test_check_is_registered_and_the_project_is_clean() -> None:
    """The check runs with the system checks and the real project passes it."""
    assert tenant_models_check in checks.registry.registry.registered_checks  # type: ignore[comparison-overlap]
    assert tenant_models_check() == []
