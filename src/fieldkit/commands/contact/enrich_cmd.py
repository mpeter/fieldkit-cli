"""fieldkit contact enrich — Discover, apply web results to, or enrich contact records.

Usage:
    fieldkit contact enrich [--account SLUG] [--json]              # run full enrichment pipeline
    fieldkit contact enrich --discover [--account SLUG] [--json]   # scan sources, write contacts-raw.json
    fieldkit contact enrich --apply-web [--account SLUG] [--json]  # merge web-search-results.json

Workflow:
    1. fieldkit contact enrich --discover   # find contacts to enrich
    2. Use Tavily/web search to populate web-search-results.json
    3. fieldkit contact enrich --apply-web  # merge results back
    4. fieldkit contact enrich              # run the enrichment pipeline
    5. fieldkit contact report              # view enrichment coverage
"""

import dataclasses
import json

import click

from fieldkit.cli_exit import cli_main
from fieldkit.contact.enrich import apply_web, discover, enrich_records


@click.command("enrich")
@click.option("--discover", "run_discover", is_flag=True, help="Scan vault files and Gmail cache for contacts")
@click.option("--apply-web", "run_apply_web", is_flag=True, help="Apply web-search-results.json to raw contacts")
@click.option("--account", default=None, metavar="SLUG", help="Restrict the operation to this account slug")
@click.option("--json", "output_json", is_flag=True, help="Emit JSON output instead of a summary")
@click.help_option("-h", "--help")
def cli(run_discover: bool, run_apply_web: bool, account: str | None, output_json: bool) -> None:
    """Discover contacts, merge web-search results, or run the enrichment pipeline.

    With no flags, runs the full enrichment pipeline (requires contacts-raw.json
    to already exist — run with --discover first).
    """
    with cli_main():
        if run_discover and run_apply_web:
            raise click.UsageError("--discover and --apply-web are mutually exclusive")

        if run_discover:
            result = discover(account=account)
            if output_json:
                click.echo(json.dumps(dataclasses.asdict(result), indent=2))
                return
            click.echo(
                f"Discovered {result.total} contacts (email={result.with_email}, linkedin={result.with_linkedin})"
            )
            if result.new_since_last_run:
                click.echo(f"New since last run: {result.new_since_last_run}")
            click.echo("By source: " + ", ".join(f"{k}={v}" for k, v in sorted(result.by_source.items())))
            return

        if run_apply_web:
            aw_result = apply_web(account=account)
            if output_json:
                click.echo(json.dumps(dataclasses.asdict(aw_result), indent=2))
                return
            if aw_result.web_results_applied == 0:
                click.echo(
                    "No web search results found. Populate web_search_results.json first "
                    "(see src/fieldkit/skills/contact/ops/contact-enrich.md for the schema and pipeline steps)."
                )
                return
            click.echo(
                f"Applied {aw_result.web_results_applied} web result(s), "
                f"updated {aw_result.updated_fields} field(s) across {aw_result.total_raw_contacts} contacts"
            )
            return

        er_result = enrich_records(account=account)
        if output_json:
            click.echo(json.dumps(dataclasses.asdict(er_result), indent=2))
            return
        if er_result.total_raw_contacts == 0:
            click.echo("No contacts to enrich. Run 'fieldkit contact enrich --discover' first.")
            return
        if er_result.migrated_legacy_files:
            click.echo(f"Migrated {er_result.migrated_legacy_files} contact file(s) from legacy memory location.")
        click.echo(f"Enriched {er_result.total_enriched} contact(s); {er_result.total_failed} failed.")
