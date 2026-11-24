"""Management command that creates demo tenants filled with fictitious billing data."""

from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from faker import Faker

from billing.seed import seed_tenant
from tenancy.models import Tenant

DEFAULT_SEED = 2026


class Command(BaseCommand):
    """Create new tenants and seed each with fictitious US billing data."""

    help = "Create demo tenants filled with fictitious US billing data."

    def add_arguments(self, parser: CommandParser) -> None:
        """Declare the command options.

        Args:
            parser: Parser to add the options to.
        """
        parser.add_argument("--tenants", type=int, default=2, help="How many tenants to create.")
        parser.add_argument(
            "--seed", type=int, default=DEFAULT_SEED, help="Base seed; tenant i uses seed + i."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Create the tenants and seed them, printing what each one received.

        Args:
            *args: Positional arguments, unused.
            **options: Parsed options.
        """
        for index in range(options["tenants"]):
            seed = options["seed"] + index
            fake = Faker("en_US")
            fake.seed_instance(seed)
            tenant = Tenant.objects.create(name=fake.company())
            counts = seed_tenant(tenant.pk, seed)
            summary = ", ".join(f"{count} {kind}" for kind, count in counts.items())
            self.stdout.write(f"{tenant.name} ({tenant.pk}): {summary}")
