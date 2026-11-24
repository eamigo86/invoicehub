"""Forms of the tenancy app."""

from django import forms


class CompanyForm(forms.Form):
    """Collects the name of a company to create.

    Attributes:
        name: Display name of the new company.
    """

    name = forms.CharField(max_length=200, label="Company name")
