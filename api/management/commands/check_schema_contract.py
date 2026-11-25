"""Compare the current GraphQL schema with a base schema and fail on breaking changes."""

from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django_graphex.settings import graphql_api_settings
from graphql.utilities import print_schema

from api.contract import breaking_changes, dangerous_changes


class Command(BaseCommand):
    """Exit non-zero when the schema breaks consumers of a base version of it."""

    help = (
        "Compare the schema generated from the code with a base SDL file. Removing what the base "
        "had already deprecated is allowed; any other breaking change fails."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        """Register the command options.

        Args:
            parser: The argparse parser supplied by Django.
        """
        parser.add_argument("--base", required=True, help="Path to the SDL of the base schema.")

    def handle(self, *args: Any, **options: Any) -> None:
        """Print dangerous changes and fail on breaking ones.

        Args:
            *args: Unused positional arguments.
            **options: Parsed options; base is the path of the base SDL.

        Raises:
            CommandError: When the schema has breaking changes against the base.
        """
        old = Path(options["base"]).read_text(encoding="utf-8")
        new = print_schema(graphql_api_settings.SCHEMA.graphql_schema)
        for description in dangerous_changes(old, new):
            self.stdout.write(self.style.WARNING(f"dangerous: {description}"))
        broken = breaking_changes(old, new)
        for description in broken:
            self.stderr.write(f"breaking: {description}")
        if broken:
            raise CommandError(f"{len(broken)} breaking change(s) against the base schema")
        self.stdout.write(self.style.SUCCESS("The schema is compatible with the base."))
