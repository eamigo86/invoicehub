"""Admin site rendering."""

from django.test import Client


def test_admin_login_uses_unfold_templates() -> None:
    """The admin login page renders with Unfold templates."""
    response = Client().get("/admin/login/")
    assert response.status_code == 200
    assert any("unfold" in (t.origin.name or "") for t in response.templates)
