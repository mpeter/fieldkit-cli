#!/usr/bin/env python3
"""
Enrich pursuit files with Gmail cache intelligence.

Generates one report per account (not per pursuit) with:
- All external contacts ranked by volume and recency
- Champion signals for top 10 contacts
- Per-pursuit keyword thread matches

Output: accounts/<acct>/gmail-intel.md
"""

import functools
import json
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict

import click

from fieldkit.commands.gmail.query import date_to_epoch, query_blindspots, query_champion_signals, query_dig
from fieldkit.config import get_account_names, get_accounts_config, get_accounts_root
from fieldkit.gmail.address_quality import account_domains, partition_suspected_masked
from fieldkit.gmail.discover import get_gmail_db_path
from fieldkit.gmail.exceptions import GmailDbNotFoundError
from fieldkit.gmail.query_domain import connect

try:
    import yaml
except ImportError:
    yaml = None

# Rolling lookback window — how far back to pull Gmail signals.
# Expressed as days so it stays current without manual updates.
_GMAIL_LOOKBACK_DAYS = 180


class EnrichAccountResult(TypedDict):
    """Machine-readable result for one configured account."""

    account: str
    status: str
    path: str | None


def _emit_enrich_error(results: list[EnrichAccountResult], *, requested: str | None, error: str) -> None:
    click.echo(json.dumps({"accounts": results, "error": error, "requested": requested}))


def _emit_enrich_header(account: str, *, as_json: bool) -> None:
    if not as_json:
        click.echo(f"\n{'=' * 60}")
        click.echo(f"Building report: {account}")
        click.echo(f"{'=' * 60}")


def _emit_enrich_complete(results: list[EnrichAccountResult], *, requested: str | None, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps({"accounts": results, "error": None, "requested": requested}, sort_keys=True))
    else:
        click.echo("\nDone.")


def _resolve_enrich_accounts(account_slug: str | None, *, as_json: bool) -> list[str] | None:
    accounts = _get_accounts()
    if account_slug is None:
        return accounts
    if account_slug in accounts:
        return [account_slug]
    if as_json:
        _emit_enrich_error([], requested=account_slug, error="account_not_found")
    else:
        click.echo(f"Warning: account '{account_slug}' not found in configured accounts.")
        click.echo(f"Known accounts: {', '.join(accounts) if accounts else '(none)'}")
    return None


def _enrich_one_account(account: str, *, as_json: bool) -> tuple[EnrichAccountResult, int]:
    _emit_enrich_header(account, as_json=as_json)
    report = build_account_report(account)
    if isinstance(report, int):
        return {"account": account, "status": "failed", "path": None}, report
    if not report:
        if not as_json:
            click.echo(f"  No pursuits found for {account}")
        return {"account": account, "status": "skipped", "path": None}, 0
    out_path = get_accounts_root() / account / "gmail-intel.md"
    _write_report(out_path, report)
    if not as_json:
        click.echo(f"  -> {out_path}")
    return {"account": account, "status": "written", "path": str(out_path)}, 0


def _write_report(path: Path, report: str) -> None:
    """Atomically replace one generated report and clean up failed temporaries."""
    fd, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(report)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _since_date() -> str:
    """Return an ISO date string for the start of the Gmail lookback window."""
    return (datetime.now(UTC) - timedelta(days=_GMAIL_LOOKBACK_DAYS)).strftime("%Y-%m-%d")


CONFIG_PATH = "config/accounts.yaml"


def load_accounts() -> list[str]:
    """Load account keys from accounts.yaml; returns [] when config is absent."""
    names = get_account_names()
    if names:
        return names
    # Fallback: try local config path
    if yaml and Path(CONFIG_PATH).exists():
        with Path(CONFIG_PATH).open(encoding="utf-8") as f:
            cfg: dict[str, Any] = yaml.safe_load(f)
        return list(cfg.get("accounts", {}).keys())
    return []


@functools.cache
def _get_accounts() -> list[str]:
    """Return account names, loading lazily on first call.

    Uses lru_cache so accounts.yaml is read at most once per process,
    and not at import time (which would crash --help when config is absent).

    Test isolation note: tests that patch ``load_accounts`` or
    ``get_account_names`` must call ``_get_accounts.cache_clear()`` in
    teardown (e.g. via a fixture or ``monkeypatch`` finalizer) to prevent
    the cached result from leaking across test cases.
    """
    return load_accounts()


def get_pursuit_keywords(pursuit_path: str) -> list[str]:
    """Extract search keywords from pursuit file name and content."""
    stem = Path(pursuit_path).stem
    text = Path(pursuit_path).read_text(encoding="utf-8")

    keywords = set()
    # From filename
    for part in stem.replace("-", " ").split():
        if len(part) >= 4 and part.lower() not in {
            "services",
            "renewal",
            "phase",
            "main",
            "enterprise",
        }:
            keywords.add(part)

    # From "What We're Selling" line
    m = re.search(r"What We're Selling.*?:\s*(.+)", text)
    if m:
        selling = m.group(1)
        for word in [
            "EDA",
            "Quay",
            "RHOAI",
            "OSV",
            "ROSA",
            "ACS",
            "AAP",
            "Ansible",
            "virtualization",
            "automation",
            "migration",
            "repave",
            "drift",
        ]:
            if word.lower() in selling.lower():
                keywords.add(word)

    return sorted(keywords)[:5]


_CHAMP_KEYWORDS = frozenset(["Threads involved", "Threads initiated", "Messages sent", "Last outbound", "Signal:"])


def _render_contacts_table(contacts_raw: list[Any], now_epoch: int) -> list[str]:
    """Render the top-contacts markdown table section."""
    lines: list[str] = ["## Top Contacts by Email Volume", ""]
    if not contacts_raw:
        return lines
    lines += ["| Contact | Messages | Recency |", "|---------|----------|---------|"]
    for email, name, msgs, last_epoch in contacts_raw[:30]:
        days = (now_epoch - last_epoch) // 86400 if last_epoch else 9999
        recency = f"{days}d ago" if days < 9999 else "unknown"
        display = f"{name} ({email})" if name and name != email else email
        lines.append(f"| {display} | {msgs} | {recency} |")
    lines.append("")
    return lines


def _render_champion_signals(conn: Any, contacts_raw: list[Any]) -> list[str]:
    """Render champion-signal subsections for top 10 contacts."""
    lines: list[str] = ["## Champion Signals (Top 10 Contacts)", ""]
    checked: set[str] = set()
    for email, name, _msgs, _epoch in contacts_raw[:10]:
        local = email.split("@")[0]
        if local in checked:
            continue
        checked.add(local)
        champ_out = query_champion_signals(conn, local)
        if not champ_out or "No people matched" in champ_out:
            continue
        display = name if name and name != email else local
        lines.append(f"### {display}")
        for raw_cl in champ_out.split("\n"):
            cl = raw_cl.strip()
            if any(k in cl for k in _CHAMP_KEYWORDS):
                lines.append(f"- {cl}")
        lines.append("")
    return lines


def _render_pursuit_threads(conn: Any, account: str, pursuits: list[str]) -> list[str]:
    """Render per-pursuit keyword thread matches."""
    lines: list[str] = ["## Per-Pursuit Thread Matches", ""]
    for p in pursuits:
        pname = Path(p).stem
        keywords = get_pursuit_keywords(p)
        lines += [f"### {pname}", f"Keywords searched: {', '.join(keywords)}", ""]
        found_any = False
        for kw in keywords:
            dig_rows = query_dig(conn, account, kw, since=date_to_epoch(_since_date()), limit=8)
            if not dig_rows:
                continue
            found_any = True
            lines += [f"**{kw}:**", "```"]
            for row in dig_rows:
                subject = (row["subject"] or "(no subject)")[:70]
                sender = (row["from_addr"] or "")[:30]
                date_str = (row["last_date"] or "")[:10]
                lines.append(f"{subject:<70}  {sender:<30}  {date_str:<10}")
            lines += ["```", ""]
        if not found_any:
            lines += ["_No matching threads found._", ""]
    return lines


def _render_checklist(pursuits: list[str]) -> list[str]:
    """Render a review checklist section for all pursuits."""
    lines: list[str] = ["## Review Checklist", ""]
    for p in pursuits:
        pname = Path(p).stem
        lines += [
            f"### {pname}",
            "- [ ] Cross-check top contacts against pursuit stakeholder list",
            "- [ ] Verify champion designation matches initiation signal",
            "- [ ] Stage email evidence for /grill against exact native ClosePlan questions",
            "- [ ] Do not write qualification state from this report",
            "",
        ]
    return lines


def build_account_report(account: str) -> str | int | None:
    """Build a gmail-intel.md report for one account by aggregating pursuit-level email signals."""
    pursuits = sorted(str(p) for p in (get_accounts_root() / account / "pursuits").glob("*.md"))
    if not pursuits:
        return None

    header = [
        f"# Gmail Intelligence Report: {account}",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}  ",
        f"**Email data since:** {_since_date()} ({_GMAIL_LOOKBACK_DAYS}d lookback)",
        "",
    ]

    try:
        conn = connect(get_gmail_db_path())
    except GmailDbNotFoundError:
        click.echo("Error: Gmail database not found. Run 'fieldkit gmail sync' first.", err=True)
        return 3
    try:
        contacts_raw = query_blindspots(conn, account, since=date_to_epoch(_since_date()), limit=None)
        domains = account_domains(get_accounts_config(), account)
        contacts, suspected = partition_suspected_masked(contacts_raw, domains)
        contacts = contacts[:40]
        now_epoch = int(datetime.now(UTC).timestamp())
        body = (
            _render_contacts_table(contacts, now_epoch)
            + ([f"_Set aside {len(suspected)} suspected masked address(es)._", ""] if suspected else [])
            + _render_champion_signals(conn, contacts)
            + _render_pursuit_threads(conn, account, pursuits)
        )
    finally:
        conn.close()

    return "\n".join(header + body + _render_checklist(pursuits))


@click.command(name="enrich-pursuits")
@click.option(
    "--account",
    "account_slug",
    default=None,
    help="Scope to a single account slug.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit enrichment outcomes as JSON.")
def cli(account_slug: str | None, as_json: bool) -> None:
    """Write gmail-intel.md files per configured account."""
    rc = _run_enrich(account_slug=account_slug, as_json=True) if as_json else _run_enrich(account_slug=account_slug)
    if rc:
        raise SystemExit(rc) from None


def _run_enrich(account_slug: str | None = None, *, as_json: bool = False) -> int:
    """Core enrich logic — called by cli().

    Args:
        account_slug: When provided, process only this account.  Warns and
            exits cleanly when the slug is not in the configured accounts list
            (implementation change).
    """
    accounts_to_run = _resolve_enrich_accounts(account_slug, as_json=as_json)
    if accounts_to_run is None:
        return 1

    results: list[EnrichAccountResult] = []
    for account in accounts_to_run:
        result, exit_code = _enrich_one_account(account, as_json=as_json)
        results.append(result)
        if exit_code:
            if as_json:
                _emit_enrich_error(results, requested=account_slug, error="report_failed")
            return exit_code

    _emit_enrich_complete(results, requested=account_slug, as_json=as_json)
    return 0


if __name__ == "__main__":
    cli()
