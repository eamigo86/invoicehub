"""The migration that turns the single customer email into the emails list keeps the data."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from billing.models import Customer
from tenancy.context import tenant_context
from tenancy.models import Tenant

pytestmark = pytest.mark.django_db(transaction=True)

BEFORE = [("billing", "0002_enable_rls_customer")]
AFTER = [("billing", "0003_customer_emails_address")]


def migrate(targets: list[tuple[str, str]]) -> MigrationExecutor:
    """Migrate the database to a target and return the executor with a fresh graph.

    Args:
        targets: Migration nodes to migrate to.

    Returns:
        An executor whose loader reflects the new state.
    """
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    return MigrationExecutor(connection)


def test_existing_emails_are_copied_into_the_list() -> None:
    """A customer with an email keeps it as the single list entry, one without gets none."""
    tenant = Tenant.objects.create(name="A")
    try:
        old_apps = migrate(BEFORE).loader.project_state(BEFORE).apps
        old_customer = old_apps.get_model("billing", "Customer")
        with tenant_context(tenant.pk):
            old_customer.objects.create(tenant_id=tenant.pk, name="With", email="a@example.com")
            old_customer.objects.create(tenant_id=tenant.pk, name="Without", email="")
        migrate(AFTER)
        with tenant_context(tenant.pk):
            emails = dict(Customer.objects.values_list("name", "emails"))
        assert emails == {"With": ["a@example.com"], "Without": []}
    finally:
        migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
