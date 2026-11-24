"""Admin site rendering."""

import pytest
from django.test import Client


@pytest.mark.django_db
def test_admin_login_uses_unfold_templates() -> None:
    """The admin login page renders with Unfold templates.

    Database access is needed because every request runs in a transaction.
    """
    response = Client().get("/admin/login/")
    assert response.status_code == 200
    assert any("unfold" in (t.origin.name or "") for t in response.templates)
