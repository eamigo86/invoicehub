"""Protect the run and proposal tables with Row-Level Security."""

from django.db import migrations

from tenancy.operations import EnableRowLevelSecurity


class Migration(migrations.Migration):
    """Enables and forces tenant isolation on the run and proposal tables."""

    dependencies = [("reconciliation", "0003_runs_and_proposals")]

    operations = [
        EnableRowLevelSecurity("ReconciliationRun"),
        EnableRowLevelSecurity("MatchProposal"),
        EnableRowLevelSecurity("MatchProposalTransaction"),
        EnableRowLevelSecurity("MatchProposalInvoice"),
    ]
