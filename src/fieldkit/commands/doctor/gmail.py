"""fieldkit doctor gmail — health check for the local gmail.db cache."""

import sqlite3
from pathlib import Path

import click

from fieldkit.cli_exit import EXIT_AUTH, EXIT_SUCCESS
from fieldkit.commands.doctor._result import DoctorResult
from fieldkit.gmail.discover import get_gmail_db_path, table_exists


def _resolve_gmail_db_path(db_path: Path | None) -> Path:
    """Return an explicit Gmail database path or the configured default."""
    return get_gmail_db_path() if db_path is None else db_path


def check_gmail(db_path: Path | None = None) -> DoctorResult:
    """Check gmail.db exists, is non-empty, and has a sound `messages` table."""
    db_path = _resolve_gmail_db_path(db_path)

    if not db_path.exists():
        return DoctorResult("gmail", healthy=False, configured=False, message="run 'fieldkit gmail sync'")

    size_bytes = db_path.stat().st_size
    if size_bytes == 0:
        return DoctorResult(
            "gmail",
            healthy=False,
            configured=True,
            message=f"{db_path} is 0 bytes — remove it and run 'fieldkit gmail sync'",
        )

    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
            message_count = row[0] if row else 0
            # historic regression: check for optional index tables built by 'fieldkit sync'
            has_people = table_exists(conn, "people")
            has_thread_accounts = table_exists(conn, "thread_accounts")
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return DoctorResult(
            "gmail",
            healthy=False,
            configured=True,
            message=f"integrity check failed ({exc}) — remove {db_path} and run 'fieldkit gmail sync'",
        )

    missing: list[str] = []
    if not has_people:
        missing.append("people")
    if not has_thread_accounts:
        missing.append("thread_accounts")

    size_mb = round(size_bytes / (1024 * 1024), 1)
    detail = f"{message_count} messages, {size_mb} MB"
    if missing:
        tables = ", ".join(missing)
        return DoctorResult(
            "gmail",
            healthy=False,
            configured=True,
            message=f"{detail} — '{tables}' table(s) missing, run 'fieldkit sync' to build",
        )
    return DoctorResult("gmail", healthy=True, configured=True, message=detail)


@click.command(name="gmail", context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    metavar="PATH",
    help="Path to gmail.db (defaults to the configured Gmail database).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit result as JSON.")
def doctor_gmail_cmd(db_path: Path | None, as_json: bool) -> None:
    """Check the local gmail.db cache for integrity.

    Exits 0 when the database is reachable and structurally sound, 2 otherwise.
    """
    result = check_gmail(db_path)
    if as_json:
        import json as _json

        click.echo(
            _json.dumps(
                {
                    "service": result.service,
                    "healthy": result.healthy,
                    "configured": result.configured,
                    "message": result.message,
                }
            )
        )
    else:
        click.echo(result.render())
    raise SystemExit(EXIT_SUCCESS if result.healthy else EXIT_AUTH)
