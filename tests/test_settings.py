"""Settings are driven by the environment, proven in clean subprocesses."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DUMP = (
    "import json, django; from django.conf import settings; "
    "print(json.dumps({'db': settings.DATABASES['default'], 'debug': settings.DEBUG, "
    "'role': settings.DATABASE_APP_ROLE, 'tz': settings.TIME_ZONE, 'tzon': settings.USE_TZ, "
    "'user_model': settings.AUTH_USER_MODEL, 'mailers': settings.MAILERS}, default=str))"
)


def run_settings(extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Load the settings in a subprocess without any dotenv file.

    Args:
        extra: Environment variables to expose to the subprocess.

    Returns:
        The completed process with captured text output.
    """
    env = {
        "PATH": os.environ["PATH"],
        "ENV_FILE": "/nonexistent/.env",
        "DJANGO_SETTINGS_MODULE": "config.settings",
        **extra,
    }
    return subprocess.run(
        [sys.executable, "-c", DUMP],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_values_come_from_environment() -> None:
    """Database, debug flag and app role are parsed from environment variables."""
    result = run_settings(
        {
            "SECRET_KEY": "k",
            "DEBUG": "true",
            "DATABASE_URL": "postgres://u:p@dbhost:6543/mydb",
            "DATABASE_APP_ROLE": "custom_role",
            "EMAIL_URL": "smtp://mailhost:2525",
        }
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    db = data["db"]
    assert (db["NAME"], db["USER"], db["HOST"], str(db["PORT"])) == ("mydb", "u", "dbhost", "6543")
    assert data["debug"] is True
    assert data["role"] == "custom_role"
    assert (data["tz"], data["tzon"]) == ("UTC", True)
    assert data["user_model"] == "accounts.User"
    assert data["mailers"]["default"]["OPTIONS"] == {"host": "mailhost", "port": 2525}


def test_defaults_when_optional_values_missing() -> None:
    """Debug defaults to off and the app role to invoicehub_app."""
    result = run_settings({"SECRET_KEY": "k", "DATABASE_URL": "postgres://u:p@h:1/d"})
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["debug"] is False
    assert data["role"] == "invoicehub_app"


def test_missing_secret_key_fails() -> None:
    """Without SECRET_KEY the settings refuse to load."""
    result = run_settings({"DATABASE_URL": "postgres://u:p@h:1/d"})
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr


def test_env_file_override_is_read(tmp_path: Path) -> None:
    """ENV_FILE points the settings at a dotenv file whose values are used.

    Args:
        tmp_path: Temporary directory provided by pytest.
    """
    dotenv = tmp_path / "custom.env"
    dotenv.write_text(
        "SECRET_KEY=from-file\nDATABASE_URL=postgres://u:p@filehost:1/filedb\nDEBUG=1\n"
    )
    env = {
        "PATH": os.environ["PATH"],
        "ENV_FILE": str(dotenv),
        "DJANGO_SETTINGS_MODULE": "config.settings",
    }
    result = subprocess.run(
        [sys.executable, "-c", DUMP],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["db"]["HOST"] == "filehost"
    assert data["debug"] is True
