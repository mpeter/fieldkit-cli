#!/usr/bin/env python3
"""Click adapter for querying the managed Gmail cache."""

import calendar
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from fieldkit.commands._account_guard import validate_account_slug
from fieldkit.config import get_accounts_config
from fieldkit.gmail import query_domain
from fieldkit.gmail.address_quality import SUSPECTED_MASKED_REASON, account_domains, partition_suspected_masked
from fieldkit.gmail.discover import get_gmail_db_path
from fieldkit.gmail.names import resolve_name

console = Console()


def _cli_connect(db_path: Path) -> sqlite3.Connection:
    """Open the canonical read-only Gmail cache for the top-level CLI boundary."""
    return query_domain.connect(db_path)


class _DateEpoch(click.ParamType[int]):
    """Click parameter type that validates YYYY-MM-DD and converts to epoch seconds.

    Using a custom ParamType rather than a post-parse conversion means Click
    owns the error path: invalid input produces a clean ``UsageError`` message
    and exits via Click's own machinery — no conflict with fieldkit's
    ``cli_main()`` exit-code taxonomy (which reserves 2 for auth failures).

    The ``is_before`` flag shifts the epoch forward by one day so that
    ``--before 2025-06-01`` means "messages before the *start* of 2025-06-02"
    (exclusive upper-bound semantics).
    """

    name = "YYYY-MM-DD"

    def __init__(self, is_before: bool = False) -> None:
        self.is_before = is_before

    def convert(self, value: str, param: click.Parameter | None, ctx: click.Context | None) -> int:
        try:
            dt = datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            self.fail(
                f"'{value}' is not a valid date. Expected YYYY-MM-DD (e.g. 2025-06-01).",
                param,
                ctx,
            )
            return 0  # unreachable; self.fail() always raises BadParameter
        epoch = calendar.timegm(dt.timetuple())
        if self.is_before:
            epoch += 86400  # exclusive upper bound: start of the next day
        return epoch


def print_results(rows: list[sqlite3.Row], label: str) -> None:
    """Print a formatted thread summary table to stdout."""
    count = len(rows)
    click.echo(f"\n{label} — {count} thread(s) found\n")
    if not rows:
        return
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("Subject", min_width=80)
    table.add_column("Last msg", min_width=10)
    table.add_column("Msgs", justify="right", min_width=4)
    for row in rows:
        subject = (row["subject"] or "(no subject)")[:80]
        # Prefer actual last message date over sync-write updated_at
        keys = row.keys()
        raw_date = (
            (row["last_date"] if "last_date" in keys else None)
            or (row["updated_at"] if "updated_at" in keys else None)
            or ""
        )
        date = query_domain._normalize_date(raw_date)
        msgs = row["message_count"]
        table.add_row(subject, date, str(msgs))
    console.print(table)


def _strip_quoted(text: str, max_chars: int = 300) -> str:
    """Strip quoted reply lines and return first meaningful chunk."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">") or (stripped.startswith("On ") and "wrote:" in stripped):
            break
        lines.append(line)
    chunk = "\n".join(lines).strip()
    # Collapse long blank runs
    chunk = re.sub(r"\n{3,}", "\n\n", chunk)
    return chunk[:max_chars]


# ---------------------------------------------------------------------------
# Shared options decorator — adds --since, --before, --limit, --db to any cmd
# ---------------------------------------------------------------------------


def _context_message(conn: sqlite3.Connection, thread_id: str, emails: list[str]) -> sqlite3.Row | None:
    """Return the most recent message in *thread_id* sent to or from *emails*."""
    msg_sql = f"""
        SELECT from_addr, to_addr, date_str, body_plain
        FROM   messages
        WHERE  thread_id = ?
          AND  ({" OR ".join(["from_addr LIKE ?" for _ in emails] + ["to_addr LIKE ?" for _ in emails])})
        ORDER BY date_epoch DESC
        LIMIT 1
    """
    msg_params = [thread_id] + [f"%{e}%" for e in emails] * 2
    row: sqlite3.Row | None = conn.execute(msg_sql, msg_params).fetchone()
    return row


def _emit_json(items: list[dict[str, Any]], filters: dict[str, Any], **extra: Any) -> None:
    """Emit a query result set as a machine-readable document on stdout.

    ``since``/``before`` appear in *filters* as the epoch integers Click parsed
    them into, not as the YYYY-MM-DD strings the operator typed: the epoch is
    what the query actually ran with, and the two differ for ``--before``
    (exclusive upper bound, shifted a day forward).
    """
    click.echo(json.dumps({"items": items, "count": len(items), **extra, "filters": filters}, indent=2, default=str))


def _shared_options(f: Any) -> Any:
    """Decorator that adds the 5 shared query options to a Click command."""
    f = click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable JSON output.")(f)
    f = click.option(
        "--db", default=lambda: str(get_gmail_db_path()), show_default="<data/gmail.db>", help="Path to gmail.db"
    )(f)
    f = click.option(
        "--limit",
        default=50,
        show_default=True,
        type=click.IntRange(min=1),
        help="Max results (default: 50; must be ≥ 1)",
    )(f)
    f = click.option(
        "--before",
        type=_DateEpoch(is_before=True),
        default=None,
        help="Include threads with messages before this date (YYYY-MM-DD)",
    )(f)
    f = click.option(
        "--since",
        type=_DateEpoch(),
        default=None,
        help="Include threads with messages on or after this date (YYYY-MM-DD)",
    )(f)
    return f


# ---------------------------------------------------------------------------
# Click group and subcommands
# ---------------------------------------------------------------------------


class _SuggestingGroup(click.Group):
    """Click Group that prints an actionable suggestion on unknown subcommands."""

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError:
            unknown = args[0] if args else "<term>"
            # args[0] was not a subcommand — treat the whole args as a search term
            search_term = " ".join(args)
            msg = (
                f"'{unknown}' is not a query subcommand.\n"
                f"Did you mean: fieldkit gmail query threads {search_term} --limit 10"
            )
            raise click.UsageError(msg) from None


@click.group(
    name="query",
    cls=_SuggestingGroup,
    invoke_without_command=True,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
)
@click.option("--account", metavar="SLUG", help="Run the account summary query for this account slug.")
@click.pass_context
def cli(ctx: click.Context, account: str | None) -> None:
    """Query gmail.db by person, account, or thread subject."""
    _run_account_alias(ctx, account)


def _run_account_alias(ctx: click.Context, account: str | None) -> None:
    if account is None:
        return
    if ctx.invoked_subcommand is not None:
        raise click.UsageError("--account cannot be combined with a query subcommand")
    ctx.invoke(cmd_account_click, name=account)


@cli.command("person")
@click.argument("name")
@_shared_options
def cmd_person_click(name: str, since: int | None, before: int | None, limit: int, db: str, as_json: bool) -> None:
    """Find threads involving a person by name or email."""
    db_path = Path(db).resolve()
    conn = _cli_connect(db_path)
    filters = {"name": name, "since": since, "before": before, "limit": limit}
    try:
        date_clause, date_params = query_domain.build_date_clause(since, before)

        # Resolve the name query via the ranked multi-strategy resolver.
        matched_rows = resolve_name(name, conn)
        emails = [r["email"] for r in matched_rows]

        if not emails:
            if as_json:
                _emit_json([], filters, matched_people=[])
            else:
                click.echo(f"No people matched '{name}'.")
            return

        like_params = [f"%{e}%" for e in emails] * 3  # from_addr, to_addr, cc_addr

        sql = f"""
            SELECT t.thread_id, t.subject, t.message_count,
                   (SELECT mx.date_str FROM messages mx
                    WHERE mx.thread_id = t.thread_id
                    ORDER BY mx.date_epoch DESC LIMIT 1) AS last_date
            FROM   threads t
            WHERE  t.thread_id IN (
                SELECT DISTINCT m.thread_id
                FROM   messages m
                WHERE  (
                           {
            " OR ".join(
                ["m.from_addr LIKE ?" for _ in emails]
                + ["m.to_addr LIKE ?" for _ in emails]
                + ["m.cc_addr LIKE ?" for _ in emails]
            )
        }
                       )
                       {date_clause}
            )
            ORDER BY last_date DESC
            LIMIT ?
        """
        params = like_params + date_params + [limit]
        rows = conn.execute(sql, params).fetchall()
        if as_json:
            _emit_json([dict(r) for r in rows], filters, matched_people=emails)
        else:
            print_results(rows, f"Person: {name}")
    finally:
        conn.close()


@cli.command("context")
@click.argument("name")
@click.option("--excerpt", default=300, show_default=True, help="Max body chars per thread (default: 300)")
@_shared_options
def cmd_context_click(
    name: str, excerpt: int, since: int | None, before: int | None, limit: int, db: str, as_json: bool
) -> None:
    """Person threads with body excerpts — pre-meeting context."""
    db_path = Path(db).resolve()
    conn = _cli_connect(db_path)
    filters = {"name": name, "excerpt": excerpt, "since": since, "before": before, "limit": limit}
    try:
        date_clause, date_params = query_domain.build_date_clause(since, before)

        people = resolve_name(name, conn)
        if not people:
            if as_json:
                _emit_json([], filters, person=name)
            else:
                click.echo(f"No people matched '{name}'.")
            return

        emails = [r["email"] for r in people]
        display = (people[0]["display_name"] or name) if people else name

        like_params = [f"%{e}%" for e in emails] * 3

        thread_sql = f"""
            SELECT t.thread_id, t.subject,
                   (SELECT mx.date_str FROM messages mx
                    WHERE mx.thread_id = t.thread_id
                    ORDER BY mx.date_epoch DESC LIMIT 1) AS last_date
            FROM   threads t
            WHERE  t.thread_id IN (
                SELECT DISTINCT m.thread_id
                FROM   messages m
                WHERE  ({
            " OR ".join(
                ["m.from_addr LIKE ?" for _ in emails]
                + ["m.to_addr LIKE ?" for _ in emails]
                + ["m.cc_addr LIKE ?" for _ in emails]
            )
        })
                       {date_clause}
            )
            ORDER BY last_date DESC
            LIMIT ?
        """
        threads = conn.execute(thread_sql, like_params + date_params + [limit]).fetchall()

        if as_json:
            items: list[dict[str, Any]] = []
            for t in threads:
                msg = _context_message(conn, t["thread_id"], emails)
                body = msg["body_plain"] if msg else None
                items.append(
                    {
                        "thread_id": t["thread_id"],
                        "subject": t["subject"],
                        "last_date": query_domain._normalize_date(t["last_date"] or ""),
                        "from_addr": msg["from_addr"] if msg else None,
                        "excerpt": _strip_quoted(body, max_chars=excerpt) if body else None,
                    }
                )
            _emit_json(items, filters, person=display)
            return

        click.echo(f"\nContext: {display} — {len(threads)} thread(s)\n")
        click.echo("=" * 80)

        for t in threads:
            click.echo(f"\n[{query_domain._normalize_date(t['last_date'] or '')}] {t['subject'] or '(no subject)'}")
            click.echo("-" * 60)

            msg = _context_message(conn, t["thread_id"], emails)
            if msg and msg["body_plain"]:
                body_excerpt = _strip_quoted(msg["body_plain"], max_chars=excerpt)
                frm = (msg["from_addr"] or "")[:60]
                click.echo(f"From: {frm}")
                click.echo(f"\n{body_excerpt}")
            else:
                click.echo("(no body text)")

        click.echo("\n" + "=" * 80)
    finally:
        conn.close()


@cli.command("account")
@click.argument("name")
@_shared_options
def cmd_account_click(name: str, since: int | None, before: int | None, limit: int, db: str, as_json: bool) -> None:
    """Show thread summary and top contacts for an account."""
    db_path = Path(db).resolve()
    conn = _cli_connect(db_path)
    try:
        account = name.lower()
        date_clause, date_params = query_domain.build_date_clause(since, before)

        if date_clause:
            sql = f"""
                SELECT t.thread_id, t.subject, t.message_count,
                       (SELECT mx.date_str FROM messages mx
                        WHERE mx.thread_id = t.thread_id
                        ORDER BY mx.date_epoch DESC LIMIT 1) AS last_date
                FROM   threads t
                JOIN   thread_accounts ta ON ta.thread_id = t.thread_id
                WHERE  LOWER(ta.account) = ?
                  AND  t.thread_id IN (
                           SELECT DISTINCT m.thread_id
                           FROM   messages m
                           WHERE  1=1 {date_clause}
                       )
                ORDER BY last_date DESC
                LIMIT ?
            """
            params = [account, *date_params, limit]
        else:
            sql = """
                SELECT t.thread_id, t.subject, t.message_count,
                       (SELECT mx.date_str FROM messages mx
                        WHERE mx.thread_id = t.thread_id
                        ORDER BY mx.date_epoch DESC LIMIT 1) AS last_date
                FROM   threads t
                JOIN   thread_accounts ta ON ta.thread_id = t.thread_id
                WHERE  LOWER(ta.account) = ?
                ORDER BY last_date DESC
                LIMIT ?
            """
            params = [account, limit]

        rows = conn.execute(sql, params).fetchall()
        if as_json:
            _emit_json(
                [dict(r) for r in rows],
                {"account": account, "since": since, "before": before, "limit": limit},
            )
        else:
            print_results(rows, f"Account: {account}")
    finally:
        conn.close()


@cli.command("dig")
@click.argument("account")
@click.argument("keyword")
@_shared_options
def cmd_dig_click(
    account: str, keyword: str, since: int | None, before: int | None, limit: int, db: str, as_json: bool
) -> None:
    """Account + keyword — deal archaeology across a full history."""
    db_path = Path(db).resolve()
    conn = _cli_connect(db_path)
    try:
        account_lower = account.lower()
        pattern = f"%{keyword}%"
        date_clause, date_params = query_domain.build_date_clause(since, before)

        if date_clause:
            sql = f"""
                SELECT t.thread_id, t.subject, t.message_count,
                       first_msg.from_addr, first_msg.date_str AS first_date,
                       (SELECT mx.date_str FROM messages mx
                        WHERE mx.thread_id = t.thread_id
                        ORDER BY mx.date_epoch DESC LIMIT 1) AS last_date
                FROM   threads t
                JOIN   thread_accounts ta ON ta.thread_id = t.thread_id
                JOIN   (
                           SELECT thread_id, from_addr, date_str
                           FROM   messages
                           WHERE  (subject LIKE ? OR body_plain LIKE ?)
                           GROUP  BY thread_id
                           HAVING MIN(date_epoch) > 0
                       ) first_msg ON first_msg.thread_id = t.thread_id
                WHERE  LOWER(ta.account) = ?
                  AND  t.thread_id IN (
                           SELECT DISTINCT thread_id FROM messages WHERE 1=1 {date_clause}
                       )
                ORDER  BY last_date DESC
                LIMIT  ?
            """
            params = [pattern, pattern, account_lower, *date_params, limit]
        else:
            sql = """
                SELECT t.thread_id, t.subject, t.message_count,
                       first_msg.from_addr, first_msg.date_str AS first_date,
                       (SELECT mx.date_str FROM messages mx
                        WHERE mx.thread_id = t.thread_id
                        ORDER BY mx.date_epoch DESC LIMIT 1) AS last_date
                FROM   threads t
                JOIN   thread_accounts ta ON ta.thread_id = t.thread_id
                JOIN   (
                           SELECT thread_id, from_addr, date_str
                           FROM   messages
                           WHERE  (subject LIKE ? OR body_plain LIKE ?)
                           GROUP  BY thread_id
                           HAVING MIN(date_epoch) > 0
                       ) first_msg ON first_msg.thread_id = t.thread_id
                WHERE  LOWER(ta.account) = ?
                ORDER  BY last_date DESC
                LIMIT  ?
            """
            params = [pattern, pattern, account_lower, limit]

        rows = conn.execute(sql, params).fetchall()
        if as_json:
            _emit_json(
                [dict(r) for r in rows],
                {"account": account_lower, "keyword": keyword, "since": since, "before": before, "limit": limit},
            )
            return

        count = len(rows)
        click.echo(f"\nDig: '{keyword}' in {account_lower} — {count} thread(s) found\n")
        if not rows:
            return
        dig_table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        dig_table.add_column("Subject", min_width=70)
        dig_table.add_column("First sender", min_width=30)
        dig_table.add_column("Last active", min_width=10)
        for row in rows:
            subject = (row["subject"] or "(no subject)")[:70]
            sender = (row["from_addr"] or "")[:30]
            date = query_domain._normalize_date(row["last_date"] or "")
            dig_table.add_row(subject, sender, date)
        console.print(dig_table)
    finally:
        conn.close()


@cli.command("threads")
@click.argument("keyword")
@click.option(
    "--account",
    "-a",
    default=None,
    metavar="SLUG",
    help="Limit results to threads tagged to a single account slug. Default: all accounts.",
)
@_shared_options
def cmd_threads_click(
    keyword: str, account: str | None, since: int | None, before: int | None, limit: int, db: str, as_json: bool
) -> None:
    """Find threads by subject keyword.

    Without --account this searches subjects across every account. With it,
    results are restricted to threads tagged to that account by
    `fieldkit gmail account-tags`, which populates thread_accounts — so a
    thread the tagger has not seen yet will not appear in a scoped search.

    Exit codes: 0 success; 3 unknown account slug.
    """
    validate_account_slug(account)
    db_path = Path(db).resolve()
    conn = _cli_connect(db_path)
    try:
        pattern = f"%{keyword}%"
        date_clause, date_params = query_domain.build_date_clause(since, before)

        # Joining thread_accounts would drop untagged threads even when no
        # account was asked for, so the join is added only when filtering.
        account_join = "JOIN   thread_accounts ta ON ta.thread_id = t.thread_id" if account else ""
        account_clause = "AND  LOWER(ta.account) = ?" if account else ""
        account_params = [account.lower()] if account else []

        if date_clause:
            sql = f"""
                SELECT t.thread_id, t.subject, t.message_count,
                       (SELECT mx.date_str FROM messages mx
                        WHERE mx.thread_id = t.thread_id
                        ORDER BY mx.date_epoch DESC LIMIT 1) AS last_date
                FROM   threads t
                {account_join}
                WHERE  t.subject LIKE ?
                  {account_clause}
                  AND  t.thread_id IN (
                           SELECT DISTINCT m.thread_id
                           FROM   messages m
                           WHERE  1=1 {date_clause}
                       )
                ORDER BY last_date DESC
                LIMIT ?
            """
            params = [pattern, *account_params, *date_params, limit]
        else:
            sql = f"""
                SELECT t.thread_id, t.subject, t.message_count,
                       (SELECT mx.date_str FROM messages mx
                        WHERE mx.thread_id = t.thread_id
                        ORDER BY mx.date_epoch DESC LIMIT 1) AS last_date
                FROM   threads t
                {account_join}
                WHERE  t.subject LIKE ?
                  {account_clause}
                ORDER BY last_date DESC
                LIMIT ?
            """
            params = [pattern, *account_params, limit]

        rows = conn.execute(sql, params).fetchall()
        if as_json:
            _emit_json(
                [dict(r) for r in rows],
                {"keyword": keyword, "account": account, "since": since, "before": before, "limit": limit},
            )
            return
        title = f"Threads: {keyword}" if account is None else f"Threads: {keyword} [{account}]"
        print_results(rows, title)
    finally:
        conn.close()


@cli.command("champion")
@click.argument("name")
@_shared_options
def cmd_champion_click(name: str, since: int | None, before: int | None, limit: int, db: str, as_json: bool) -> None:
    """Champion signal: initiation rate, last contact, thread history."""
    db_path = Path(db).resolve()
    conn = _cli_connect(db_path)
    try:
        report = query_domain.query_champion_signals(conn, name, since=since)
        if as_json:
            # The domain returns a rendered report, not a record set, so JSON
            # wraps it rather than inventing a structure the domain doesn't have.
            click.echo(
                json.dumps(
                    {
                        "name": name,
                        "report": report,
                        "filters": {"name": name, "since": since, "before": before, "limit": limit},
                    },
                    indent=2,
                    default=str,
                )
            )
        else:
            click.echo(report)
    finally:
        conn.close()


def _format_blindspots_table(results: list[dict[str, Any]], *, show_quality: bool = False) -> None:
    """Print the formatted blindspots table for *results*."""
    bs_table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    bs_table.add_column("Email", min_width=40)
    bs_table.add_column("Name", min_width=25)
    bs_table.add_column("Msgs", justify="right", min_width=4)
    bs_table.add_column("Last seen", min_width=12)
    if show_quality:
        bs_table.add_column("Quality", min_width=16)
    for r in results:
        quality_reason = r.get("quality_reason")
        email = f"SUSPECTED: {r['email']}" if show_quality and quality_reason else r["email"]
        email = email[:40]
        name = r["name"][:25]
        last = f"{r['days']}d ago" if r["days"] < 9999 else "unknown"
        row = [email, name, str(r["msgs"]), last]
        if show_quality:
            row.append("suspected masked" if quality_reason else "ordinary")
        bs_table.add_row(*row)
    console.print(bs_table)
    click.echo("\n  Tip: cross-reference against account.md stakeholder list to find gaps.")


@cli.command("blindspots")
@click.argument("account")
@click.option("--min-messages", default=3, show_default=True, help="Minimum message count to surface (default: 3)")
@click.option(
    "--known",
    metavar="EMAIL_FRAGMENTS",
    default=None,
    help="Comma-separated email fragments to exclude (known contacts)",
)
@click.option(
    "--include-suspected",
    is_flag=True,
    default=False,
    help="Include and mark addresses suspected of containing masked data.",
)
@_shared_options
def cmd_blindspots_click(
    account: str,
    min_messages: int,
    known: str | None,
    include_suspected: bool,
    since: int | None,
    before: int | None,
    limit: int,
    db: str,
    as_json: bool,
) -> None:
    """People active in account email but not in account.md stakeholder lists."""
    db_path = Path(db).resolve()
    conn = _cli_connect(db_path)
    filters = {
        "account": account.lower(),
        "min_messages": min_messages,
        "known": known,
        "include_suspected": include_suspected,
        "since": since,
        "before": before,
        "limit": limit,
    }
    try:
        account_lower = account.lower()
        now_epoch = int(datetime.now(UTC).timestamp())
        known_fragments = [k.strip().lower() for k in known.split(",")] if known else []

        raw_results = query_domain.query_blindspots(conn, account_lower, since=since, limit=None)
        if not raw_results:
            if as_json:
                _emit_json([], filters, suspected_items=[], suspected_count=0)
            else:
                click.echo(f"\nNo threads found for account: {account_lower}")
            return

        filtered: list[tuple[str, str, int, int]] = []
        for email, name, msgs, last_epoch in raw_results:
            if msgs < min_messages:
                continue
            if any(f in email for f in known_fragments):
                continue
            filtered.append((email, name, msgs, last_epoch))

        domains = account_domains(get_accounts_config(), account_lower)
        ordinary, suspected = partition_suspected_masked(filtered, domains)

        def render_record(record: tuple[str, str, int, int], *, suspected_masked: bool = False) -> dict[str, Any]:
            email, name, msgs, last_epoch = record
            days = (now_epoch - last_epoch) // 86400 if last_epoch else 9999
            result: dict[str, Any] = {"email": email, "name": name, "msgs": msgs, "days": days}
            if suspected_masked:
                result["quality_reason"] = SUSPECTED_MASKED_REASON
            return result

        ordinary_items = [render_record(record) for record in ordinary]
        suspected_items = [render_record(record, suspected_masked=True) for record in suspected]
        selected_items = ordinary_items + suspected_items if include_suspected else ordinary_items

        selected_items.sort(key=lambda item: item["days"])
        results = selected_items[:limit]

        if as_json:
            _emit_json(
                results,
                filters,
                suspected_items=suspected_items,
                suspected_count=len(suspected_items),
            )
            return

        click.echo(f"\nBlind spots: {account_lower} — {len(results)} external contact(s) found\n")
        if not results:
            click.echo("  (no results — try --min-messages 1)")
            if suspected_items and not include_suspected:
                click.echo(
                    f"\n  Set aside {len(suspected_items)} suspected masked address(es). "
                    "Use --include-suspected to inspect them."
                )
            return

        _format_blindspots_table(results, show_quality=include_suspected)
        if suspected_items and not include_suspected:
            click.echo(
                f"\n  Set aside {len(suspected_items)} suspected masked address(es). "
                "Use --include-suspected to inspect them."
            )
    finally:
        conn.close()
