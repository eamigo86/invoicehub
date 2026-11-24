"""Test-only URLconf combining the project URLs with the tenant probe view."""

from django.urls import path

from config.urls import urlpatterns as project_urlpatterns
from tests.urls_tenant_probe import probe

urlpatterns = [*project_urlpatterns, path("probe/", probe)]
