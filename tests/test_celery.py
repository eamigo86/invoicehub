"""Celery application wiring."""

from django.conf import settings

from config.celery import app


def test_broker_url_comes_from_django_settings() -> None:
    """The Celery broker URL is the CELERY_BROKER_URL Django setting."""
    assert app.conf.broker_url == settings.CELERY_BROKER_URL
