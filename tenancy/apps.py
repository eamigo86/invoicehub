"""App configuration for tenancy."""

from django.apps import AppConfig
from django.core import checks


class TenancyConfig(AppConfig):
    """Configuration for the tenancy app."""

    name = "tenancy"

    def ready(self) -> None:
        """Register the tenant model system checks."""
        from tenancy import signals  # noqa: F401  (connects the login receiver)
        from tenancy.checks import tenant_models_check

        checks.register(tenant_models_check)
