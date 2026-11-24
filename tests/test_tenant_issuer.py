"""Issuer details on the tenant and the shared pieces they are built from."""

import pytest
from django.core.exceptions import ValidationError

from core.models import Currency, PostalAddress
from tenancy.models import Tenant

pytestmark = pytest.mark.django_db


def test_currency_choices_are_the_north_american_currencies() -> None:
    """The supported currencies are USD, CAD and MXN."""
    assert [c.value for c in Currency] == ["USD", "CAD", "MXN"]


def test_postal_address_is_abstract_and_defaults_to_the_us() -> None:
    """The address mixin is abstract, and an empty address is in the United States."""
    assert PostalAddress._meta.abstract
    assert Tenant().country == "US"
    assert Tenant().address_line1 == ""


def test_a_new_tenant_has_us_issuer_defaults() -> None:
    """Issuer fields default to USD, New York time and empty legal data."""
    tenant = Tenant.objects.create(name="Acme")
    assert tenant.default_currency == Currency.USD
    assert tenant.timezone == "America/New_York"
    assert tenant.legal_name == ""
    assert tenant.ein == ""


def test_a_valid_issuer_passes_validation() -> None:
    """A well-formed EIN, currency and timezone are accepted."""
    tenant = Tenant(
        name="Acme", legal_name="Acme LLC", ein="12-3456789", default_currency="CAD",
        timezone="America/Toronto",
    )  # fmt: skip
    tenant.full_clean()


@pytest.mark.parametrize(
    "ein", ["123456789", "1-23456789", "12-345678", "ab-cdefghi", "12-3456789x"]
)
def test_a_malformed_ein_is_rejected(ein: str) -> None:
    """An EIN must look like NN-NNNNNNN.

    Args:
        ein: Malformed value.
    """
    with pytest.raises(ValidationError) as caught:
        Tenant(name="Acme", ein=ein).full_clean()
    assert "ein" in caught.value.message_dict


def test_an_unknown_timezone_is_rejected() -> None:
    """The timezone must be an IANA name known to zoneinfo."""
    with pytest.raises(ValidationError) as caught:
        Tenant(name="Acme", timezone="Mars/Olympus").full_clean()
    assert "timezone" in caught.value.message_dict


def test_an_unknown_currency_is_rejected() -> None:
    """Only the supported currencies are accepted."""
    with pytest.raises(ValidationError) as caught:
        Tenant(name="Acme", default_currency="EUR").full_clean()
    assert "default_currency" in caught.value.message_dict
