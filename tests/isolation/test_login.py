"""Logging in through allauth never activates a tenant by itself."""

import pytest
from allauth.account.models import EmailAddress
from django.core.cache import cache
from django.test import Client

from accounts.models import User
from tenancy.models import Tenant

pytestmark = [
    pytest.mark.isolation,
    pytest.mark.django_db(transaction=True),
    pytest.mark.urls("tests.urls_login_probe"),
]


def test_login_drops_a_pre_login_session_tenant_the_user_does_not_belong_to(
    client: Client,
) -> None:
    """A tenant planted in the anonymous session is not honored after an allauth login.

    Args:
        client: Django test client fixture.
    """
    cache.clear()
    user = User.objects.create_user("eve", "eve@example.com", "S3cure-passw0rd!")
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    tenant = Tenant.objects.create(name="A")
    session = client.session
    session["tenant_id"] = str(tenant.pk)
    session.save()
    response = client.post(
        "/accounts/login/", {"login": "eve@example.com", "password": "S3cure-passw0rd!"}
    )
    assert response.status_code == 302
    assert client.session["_auth_user_id"] == str(user.pk)
    assert client.get("/probe/").content.decode() == ""
    assert "tenant_id" not in client.session
