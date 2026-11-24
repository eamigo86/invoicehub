"""Registry of tenant models and one row factory for each.

Adding a tenant model without a factory here fails the meta-test, which is how every new model
gets its isolation cases: the parametrized suite runs against whatever this registry holds.
"""

import uuid
from collections.abc import Callable

from django.apps import apps

from billing.models import Customer
from tenancy.models import TenantModel


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
