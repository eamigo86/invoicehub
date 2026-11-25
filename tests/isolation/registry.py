"""Registry of tenant models and one row factory for each.

Adding a tenant model without a factory here fails the meta-test, which is how every new model
gets its isolation cases: the parametrized suite runs against whatever this registry holds.
"""

import importlib
import importlib.util
import inspect
import pkgutil
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from celery import Celery
from django.apps import apps
from django.apps.registry import Apps
from django.db import models
from django.urls import URLPattern, URLResolver

from billing.models import Customer, Product, ProductTaxRate, TaxRate
from tenancy.models import ApiToken, Membership, Tenant, TenantModel


def make_customer() -> TenantModel:
    """Create one customer for the active tenant.

    Returns:
        The saved customer.
    """
    customer: TenantModel = Customer.objects.create(name=f"customer-{uuid.uuid4().hex[:8]}")
    return customer


def make_tax_rate() -> TenantModel:
    """Create one tax rate for the active tenant.

    Returns:
        The saved tax rate.
    """
    tax_rate: TenantModel = TaxRate.objects.create(name=f"rate-{uuid.uuid4().hex[:8]}", rate_bp=825)
    return tax_rate


def make_product() -> TenantModel:
    """Create one product for the active tenant.

    Returns:
        The saved product.
    """
    product: TenantModel = Product.objects.create(
        name=f"product-{uuid.uuid4().hex[:8]}", unit_price=100
    )
    return product


def make_product_tax_rate() -> TenantModel:
    """Create one product with one tax rate, linked, for the active tenant.

    Returns:
        The saved link.
    """
    link: TenantModel = ProductTaxRate.objects.create(
        product=make_product(), tax_rate=make_tax_rate()
    )
    return link


FACTORIES: dict[type[TenantModel], Callable[[], TenantModel]] = {
    Customer: make_customer,
    TaxRate: make_tax_rate,
    Product: make_product,
    ProductTaxRate: make_product_tax_rate,
}

ADMIN_MARKER_FIELDS: dict[type[TenantModel], str] = {
    Customer: "name",
    TaxRate: "name",
    Product: "name",
}
"""Plain text field of each model's admin form that the isolation tests overwrite with a marker.

Every other form value comes from the admin's own change form, so the POST stays valid."""


def clone_values(model: type[TenantModel]) -> dict[str, Any]:
    """Pick column values that let a second row of the active tenant coexist with a factory row.

    Tests that copy a row use them so that unique constraints are not violated. A model with a
    marker text field gets a fresh text; a link model gets another row to point at.

    Args:
        model: Tenant model under test.

    Returns:
        Column names mapped to the values that replace the copied ones.
    """
    if model is ProductTaxRate:
        return {"tax_rate_id": make_tax_rate().pk}
    return {ADMIN_MARKER_FIELDS.get(model, "name"): f"clone-{uuid.uuid4().hex}"}


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
    ApiToken: "looked up by token hash before any tenant is active, to resolve a request tenant",
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


def project_url_names(
    patterns: Sequence[URLPattern | URLResolver],
    skip_namespaces: tuple[str, ...] = ("admin",),
    skip_modules: tuple[str, ...] = ("allauth",),
) -> list[str]:
    """List the named URLs of a URLconf that belong to the project's own views.

    Args:
        patterns: URL patterns to walk, such as the urlpatterns of the root URLconf.
        skip_namespaces: Included namespaces to leave out, the admin site by default.
        skip_modules: Top-level packages whose views are left out, allauth by default.

    Returns:
        The reversible names, prefixed with their namespaces, in declaration order.
    """
    names: list[str] = []
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            if pattern.namespace in skip_namespaces:
                continue
            prefix = f"{pattern.namespace}:" if pattern.namespace else ""
            inner = project_url_names(pattern.url_patterns, skip_namespaces, skip_modules)
            names.extend(f"{prefix}{name}" for name in inner)
        elif pattern.name and pattern.callback.__module__.split(".")[0] not in skip_modules:
            names.append(pattern.name)
    return names


URL_ISOLATION_CASES: frozenset[str] = frozenset({"tenancy:list", "tenancy:select"})
"""Names of the project URLs that have isolation cases; add every new project URL name here.

The cases for the tenancy URLs are in test_tenant_selection."""


def project_service_functions(
    registry: Apps = apps, labels: tuple[str, ...] = BUSINESS_APPS
) -> list[str]:
    """List the public functions defined in the services module of each business app.

    Imported names are left out, and apps without a services module are skipped.

    Args:
        registry: App registry to inspect.
        labels: Business app labels to inspect, which must all be installed in the registry.

    Returns:
        The dotted names such as billing.services.save_customer, ordered.
    """
    names: list[str] = []
    for label in labels:
        module_name = f"{registry.get_app_config(label).name}.services"
        if importlib.util.find_spec(module_name) is not None:
            names.extend(service_functions(module_name))
    return sorted(names)


def service_functions(module_name: str) -> list[str]:
    """List the public functions defined in a services module, or in every module of a package.

    Args:
        module_name: Dotted name of the services module or package.

    Returns:
        The dotted names of the functions defined there, imported names left out, ordered.
    """
    module = importlib.import_module(module_name)
    modules = [module]
    if hasattr(module, "__path__"):
        modules += [
            importlib.import_module(info.name)
            for info in pkgutil.walk_packages(module.__path__, f"{module_name}.")
        ]
    return sorted(
        f"{mod.__name__}.{name}"
        for mod in modules
        for name, func in inspect.getmembers(mod, inspect.isfunction)
        if func.__module__ == mod.__name__ and not name.startswith("_")
    )


SERVICE_ISOLATION_CASES: frozenset[str] = frozenset(
    {
        "billing.services.save_customer",
        "billing.services.delete_customer",
        "billing.services.update_issuer",
        "billing.services.save_tax_rate",
        "billing.services.delete_tax_rate",
        "billing.services.save_product",
        "billing.services.delete_product",
    }
)
"""Dotted names of the service functions that have isolation cases; add every new one here.

The cases are in test_services."""


def project_task_names(celery_app: Celery) -> list[str]:
    """List the Celery tasks registered by project modules.

    Args:
        celery_app: Celery application whose task registry is inspected, after autodiscovery.

    Returns:
        The task names, built-in celery.* tasks left out, ordered.
    """
    celery_app.loader.import_default_modules()
    return sorted(name for name in celery_app.tasks if not name.startswith("celery."))


TASK_ISOLATION_CASES: frozenset[str] = frozenset()
"""Names of the Celery tasks that have isolation cases; add every new task here."""
