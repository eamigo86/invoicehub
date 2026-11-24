"""Account models."""

from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """Custom user model, kept identical to the default until fields are needed."""
