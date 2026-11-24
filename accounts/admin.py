"""Admin registration for users and groups using Unfold."""

from allauth.account.decorators import secure_admin_login
from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group
from unfold.admin import ModelAdmin  # type: ignore[import-untyped]
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm  # type: ignore[import-untyped]

from .models import User

admin.site.unregister(Group)
# The admin has no login form of its own: anonymous visitors go to allauth, staff pass through.
admin.site.login = secure_admin_login(admin.site.login)  # type: ignore[method-assign]


@admin.register(User)
class UserAdmin(BaseUserAdmin, ModelAdmin):  # type: ignore[misc,type-arg]
    """Unfold-styled admin for the custom user model."""

    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm


@admin.register(Group)
class GroupAdmin(BaseGroupAdmin, ModelAdmin):  # type: ignore[misc]
    """Unfold-styled admin for groups."""
