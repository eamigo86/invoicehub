"""URLs of the GraphQL API."""

from django.urls import path

from api import views

app_name = "api"

urlpatterns = [path("graphql/", views.graphql, name="graphql")]
