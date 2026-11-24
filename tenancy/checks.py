"""System checks that keep tenant models safe from cross-tenant uniqueness.

A unique constraint that ignores the tenant would let one tenant's data block or reveal
another's, so every uniqueness rule on a tenant model must include the tenant.
"""

from collections.abc import Iterable
from typing import Any

from django.apps import apps
from django.core.checks import CheckMessage, Error
from django.db import models

from tenancy.models import TenantModel

TENANT_FIELDS = {"tenant", "tenant_id"}


def check_tenant_models(model_classes: Iterable[type[models.Model]]) -> list[CheckMessage]:
    """Check the uniqueness rules of concrete tenant models.

    Reports tenancy.E001 for a unique constraint or unique_together without the tenant, and
    tenancy.E002 for a non-primary-key field declared unique.

    Args:
        model_classes: Models to inspect; anything that is not a concrete TenantModel is skipped.

    Returns:
        One error per violation, empty when every model is correct.
    """
    errors: list[CheckMessage] = []
    for model in model_classes:
        if not issubclass(model, TenantModel) or model._meta.abstract:
            continue
        rules = [
            (c.name, c.fields)
            for c in model._meta.constraints
            if isinstance(c, models.UniqueConstraint)
        ]
        rules += [(f"unique_together {fields}", fields) for fields in model._meta.unique_together]
        errors += [
            Error(
                f"{name} of {model.__name__} must include the tenant field.",
                obj=model,
                id="tenancy.E001",
            )
            for name, fields in rules
            if not TENANT_FIELDS.intersection(fields)
        ]
        errors += [
            Error(
                f"Field {field.name} of {model.__name__} must not be unique on its own; "
                "uniqueness has to include the tenant.",
                obj=field,
                id="tenancy.E002",
            )
            for field in model._meta.local_fields
            if field.unique and not field.primary_key
        ]
    return errors


def tenant_models_check(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    """Run the tenant model checks over every installed model.

    Args:
        app_configs: App configs selected by the check framework, unused.
        **kwargs: Extra arguments passed by the check framework, unused.

    Returns:
        The errors found in the installed models.
    """
    return check_tenant_models(apps.get_models())
