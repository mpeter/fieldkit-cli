"""fieldkit doctor gmail — health check for the local gmail.db cache."""

import sqlite3
from pathlib import Path

import click

from fieldkit.commands.doctor._result import DoctorResult
from fieldkit.config import ConfigError
from fieldkit.errors import GmailSyncPartialError, SQLiteSnapshotError
from fieldkit.gmail import query_domain
from fieldkit.gmail.discover import get_gmail_db_path
from fieldkit.gmail.exceptions import GmailDbNotFoundError, GmailSchemaError


def _resolve_gmail_db_path(db_path: Path | None) -> Path:
    """Return an explicit Gmail database path or the configured default."""
    return get_gmail_db_path() if db_path is None else db_path


def _unreadable_cache() -> DoctorResult:
    """Keep an unreadable cache intact and distinguish data from credentials."""
    return DoctorResult(
        "gmail",
        healthy=False,
        failure_kind="data",
        configured=True,
        message=(
            "The cache could not be verified without application writes — preserve it, stop cache users, "
            "and check its path and permissions; "
            "if damaged, stop cache users and move a recoverable backup aside before running 'fieldkit gmail sync'"
        ),
    )


def _active_cache() -> DoctorResult:
    return DoctorResult(
        "gmail",
        healthy=False,
        failure_kind="retryable",
        configured=True,
        message="The cache is active or not ready — retry after the current sync finishes",
    )


def check_gmail(db_path: Path | None = None) -> DoctorResult:
    """Check the local cache is readable, non-empty, and has the required tables."""
    try:
        db_path = _resolve_gmail_db_path(db_path)
    except (ConfigError, ValueError, OSError):
        return _unreadable_cache()

    try:
        size_bytes = db_path.stat().st_size
    except FileNotFoundError:
        return DoctorResult(
            "gmail", healthy=False, failure_kind="data", configured=False, message="run 'fieldkit gmail sync'"
        )
    except OSError:
        return _unreadable_cache()

    if size_bytes == 0:
        return DoctorResult(
            "gmail",
            healthy=False,
            failure_kind="data",
            configured=True,
            message="The cache is empty — preserve a backup before rebuilding with 'fieldkit gmail sync'",
        )

    try:
        conn = query_domain.connect(db_path)
        try:
            (message_count,) = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        finally:
            conn.close()
    except GmailSyncPartialError:
        return _active_cache()
    except SQLiteSnapshotError as exc:
        return _active_cache() if exc.reason in {"active", "resource"} else _unreadable_cache()
    except (GmailDbNotFoundError, GmailSchemaError, OSError, ValueError, sqlite3.DatabaseError):
        return _unreadable_cache()

    size_mb = round(size_bytes / (1024 * 1024), 1)
    detail = f"{message_count} messages, {size_mb} MB"
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
    """Check readability and required tables in the local gmail.db cache.

    Exits 0 when healthy, 1 when an active writer makes the check retryable, and
    3 for invalid or unverified cache data.
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
    raise SystemExit(result.exit_code)
