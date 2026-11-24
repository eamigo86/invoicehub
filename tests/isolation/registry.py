"""Registry of tenant models and one row factory for each.

Adding a tenant model without a factory here fails the meta-test, which is how every new model
gets its isolation cases: the parametrized suite runs against whatever this registry holds.
"""

import uuid
from collections.abc import Callable

from django.apps import apps
from django.apps.registry import Apps
from django.db import models

from billing.models import Customer
from tenancy.models import Membership, Tenant, TenantModel


def make_customer() -> TenantModel:
    """Create one customer for the active tenant.

    Returns:
        The saved customer.
    """
    customer: TenantModel = Customer.objects.create(name=f"customer-{uuid.uuid4().hex[:8]}")
    return customer


FACTORIES: dict[type[TenantModel], Callable[[], TenantModel]] = {Customer: make_customer}


def concrete_tenant_models() -> list[type[TenantModel]]:
    """List the installed concrete tenant models.

    Returns:
        Every non-abstract TenantModel subclass registered with Django, ordered by name.
    """
    found = [
        m
        for m in apps.get_models()
        if issubclass(m, TenantModel) and not m._meta.abstract and not m._meta.proxy
    ]
    return sorted(found, key=lambda m: m.__name__)


BUSINESS_APPS = ("billing", "reconciliation", "assistant")
"""Apps whose models hold business data; add every future business app here."""

NON_TENANT_ALLOWLIST: frozenset[str] = frozenset()
"""Labels such as billing.country of models deliberately shared by all tenants."""


def non_tenant_models(registry: Apps = apps, labels: tuple[str, ...] = BUSINESS_APPS) -> list[str]:
    """List business models that are not tenant models and are not allowlisted.

    Auto-created many-to-many through tables are included, so a business model cannot use an
    implicit through table and has to declare an explicit tenant-scoped one.

    Args:
        registry: App registry to inspect.
        labels: Business app labels to inspect, which must all be installed in the registry.

    Returns:
        The labels of the offending models, ordered.
    """
    offenders = [
        model._meta.label
        for label in labels
        for model in registry.get_app_config(label).get_models(include_auto_created=True)
        if not issubclass(model, TenantModel) and model._meta.label not in NON_TENANT_ALLOWLIST
    ]
    return sorted(offenders)


PROJECT_APPS = ("core", "tenancy", "accounts", "billing", "reconciliation", "assistant")
"""The project's own apps, scanned for models with a foreign key to the tenant."""

TENANT_FK_ALLOWLIST: dict[type[models.Model], str] = {
    Membership: "looked up before a tenant is active, to decide which tenants a user may enter",
}
"""Plain models that may point at a tenant without being a TenantModel, each with its reason."""


def unprotected_tenant_fk_models(
    registry: Apps = apps,
    labels: tuple[str, ...] = PROJECT_APPS,
    allowlist: dict[type[models.Model], str] = TENANT_FK_ALLOWLIST,
) -> list[str]:
    """List models with a foreign key to Tenant that are neither TenantModel nor allowlisted.

    Args:
        registry: App registry to inspect.
        labels: App labels to inspect, which must all be installed in the registry.
        allowlist: Plain models allowed to reference a tenant, each mapped to its reason.

    Returns:
        The labels of the offending models, ordered.
    """
    offenders = [
        model._meta.label
        for label in labels
        for model in registry.get_app_config(label).get_models(include_auto_created=True)
        if not issubclass(model, TenantModel)
        and model not in allowlist
        and any(
            isinstance(f, models.ForeignKey) and f.related_model is Tenant
            for f in model._meta.local_fields
        )
    ]
    return sorted(offenders)
