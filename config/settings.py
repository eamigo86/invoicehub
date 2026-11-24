"""Django settings, fully driven by environment variables.

A dotenv file is read when present; its path defaults to .env at the project root and can be
overridden with the ENV_FILE variable.
"""

import os
from pathlib import Path

import environ  # type: ignore[import-untyped]

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
_env_file = Path(os.environ.get("ENV_FILE", BASE_DIR / ".env"))
if _env_file.is_file():
    environ.Env.read_env(_env_file)

SECRET_KEY: str = env("SECRET_KEY")
DEBUG: bool = env.bool("DEBUG", default=False)
ALLOWED_HOSTS: list[str] = env.list("ALLOWED_HOSTS", default=[])

DATABASES = {"default": env.db("DATABASE_URL")}
DATABASE_APP_ROLE: str = env("DATABASE_APP_ROLE", default="invoicehub_app")
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CELERY_BROKER_URL: str = env("CELERY_BROKER_URL", default="redis://localhost:6390/0")

_email = env.email("EMAIL_URL", default="consolemail://")
_smtp = {
    "host": "EMAIL_HOST",
    "port": "EMAIL_PORT",
    "username": "EMAIL_HOST_USER",
    "password": "EMAIL_HOST_PASSWORD",
}
MAILERS = {
    "default": {
        "BACKEND": _email["EMAIL_BACKEND"],
        "OPTIONS": {k: _email[v] for k, v in _smtp.items() if _email[v]}
        if _email["EMAIL_HOST"]
        else {},
    }
}

INSTALLED_APPS = [
    "unfold",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
    "tenancy",
    "accounts",
    "billing",
    "reconciliation",
    "assistant",
]

AUTH_USER_MODEL = "accounts.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

UNFOLD = {"SITE_TITLE": "InvoiceHub", "SITE_HEADER": "InvoiceHub"}
