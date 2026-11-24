"""URLs of the tenant selection page."""

from django.urls import path

from tenancy import views

app_name = "tenancy"

urlpatterns = [
    path("", views.tenant_list, name="list"),
    path("<uuid:tenant_id>/select/", views.tenant_select, name="select"),
]
