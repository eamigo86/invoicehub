"""Shared pytest configuration: Hypothesis profiles, a temporary media root and no broker."""

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from hypothesis import settings

settings.register_profile("default", deadline=None)
settings.register_profile("ci", derandomize=True, deadline=None)
settings.load_profile("ci" if os.environ.get("CI") else "default")


@pytest.fixture(autouse=True)
def media_root(settings: Any, tmp_path: Path) -> Path:
    """Point the file storage at a temporary directory, so tests never write into the project.

    Args:
        settings: Pytest-django settings fixture.
        tmp_path: Temporary directory of the test.

    Returns:
        The directory used as MEDIA_ROOT.
    """
    settings.MEDIA_ROOT = tmp_path / "media"
    return Path(settings.MEDIA_ROOT)


@pytest.fixture(autouse=True)
def enqueued(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the delay of the PDF task, so tests never reach a broker.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The mock standing in for delay.
    """
    from billing.tasks import render_invoice_pdf

    delay = MagicMock()
    monkeypatch.setattr(render_invoice_pdf, "delay", delay)
    return delay
