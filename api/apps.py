"""App configuration for the GraphQL API."""

from django.apps import AppConfig


class ApiConfig(AppConfig):
    """Configuration of the api app, which holds the schema and the endpoint but no models."""

    name = "api"
