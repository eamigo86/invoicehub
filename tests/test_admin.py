"""Admin site rendering."""

import pytest
from django.test import Client


@pytest.mark.django_db
def test_admin_login_is_the_allauth_login_in_the_unfold_layout() -> None:
    """An anonymous admin visit ends on the allauth login rendered with Unfold templates.

    Database access is needed because every request runs in a transaction.
    """
    response = Client().get("/admin/login/", follow=True)
    assert response.redirect_chain[0][0].startswith("/accounts/login/")
    assert response.status_code == 200
    assert any("unfold" in (t.origin.name or "") for t in response.templates)
