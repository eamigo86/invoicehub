"""Django settings, fully driven by environment variables.

A dotenv file is read when present; its path defaults to .env at the project root and can be
overridden with the ENV_FILE variable.
"""

import os
from pathlib import Path

import environ  # type: ignore[import-untyped]

from config.mail import build_mailers
from core.roles import validate_role_name

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
_env_file = Path(os.environ.get("ENV_FILE", BASE_DIR / ".env"))
if _env_file.is_file():
    environ.Env.read_env(_env_file)

SECRET_KEY: str = env("SECRET_KEY")
DEBUG: bool = env.bool("DEBUG", default=False)
ALLOWED_HOSTS: list[str] = env.list("ALLOWED_HOSTS", default=[])

DATABASES = {"default": env.db("DATABASE_URL")}
DATABASES["default"]["ENGINE"] = "tenancy.db_backend"
DATABASES["default"]["ATOMIC_REQUESTS"] = True
DATABASE_APP_ROLE: str = validate_role_name(env("DATABASE_APP_ROLE", default="invoicehub_app"))
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CELERY_BROKER_URL: str = env("CELERY_BROKER_URL", default="redis://localhost:6390/0")

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        # A different Redis database than the Celery broker.
        "LOCATION": env("CACHE_URL", default="redis://localhost:6390/1"),
    }
}
GRAPHQL_RATE_LIMIT_PER_MINUTE: int = env.int("GRAPHQL_RATE_LIMIT_PER_MINUTE", default=600)

MAILERS = build_mailers(env("EMAIL_URL", default="consolemail://"))
DEFAULT_FROM_EMAIL: str = env("DEFAULT_FROM_EMAIL", default="InvoiceHub <no-reply@localhost>")

INSTALLED_APPS = [
    "unfold",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",  # ArrayField of tenancy.ApiToken
    "allauth",
    "allauth.account",
    "django_graphex",
    "core",
    "tenancy",
    "accounts",
    "billing",
    "reconciliation",
    "assistant",
    "api",
]

AUTH_USER_MODEL = "accounts.User"

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

# allauth: email and password only; the username stays on the model and allauth fills it in.
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_UNIQUE_EMAIL = True
# Interim target until the tenant selection page exists.
LOGIN_REDIRECT_URL = "tenancy:list"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "tenancy.middleware.TenantMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.allauth_unfold_context",
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

# Uploaded and generated files (invoice PDFs). Local disk for now; GCS comes with the infra slice.
MEDIA_ROOT = Path(env("MEDIA_ROOT", default=str(BASE_DIR / "media")))
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

UNFOLD = {
    "SITE_TITLE": "InvoiceHub",
    "SITE_HEADER": "InvoiceHub",
    "SITE_URL": "/tenants/",
    "SITE_DROPDOWN": [{"icon": "swap_horiz", "title": "Switch tenant", "link": "/tenants/"}],
}

DJANGO_GRAPHEX = {
    "SCHEMA": "api.schema.schema",
    "DEFAULT_PAGE_SIZE": 50,
    "MAX_PAGE_SIZE": 100,
    # Depth: the deepest path of the schema is five levels (invoices, results, allocations,
    # results, payment), so 6 leaves one level of slack. Cost: graphex charges a list wrapper
    # and its results page both, so the RFC overdue-invoices query (a page of 50 with customer,
    # lines and allocations) is rated 51,015,101; the budget is about twice that, and aliasing
    # that shape even twice is refused.
    "MAX_QUERY_DEPTH": 6,
    "MAX_QUERY_COST": 100_000_000,
}
