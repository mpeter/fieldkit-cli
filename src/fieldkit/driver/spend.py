"""fieldkit.driver.spend — LLM spend summary for driver-loop PR bodies.

Queries ``llm-calls.db`` for rows tagged with the current driver run's issue
number (stored in the ``account`` column as ``driver-issue-NNN``) and returns
a one-line cost note suitable for embedding in a PR description.

The ``account`` column is repurposed as a run tag here because ``llm_calls``
has no dedicated ``run_id`` column.  The tag format is ``driver-issue-NNN``
(e.g. ``driver-issue-42``).
"""

import logging
import os
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path

from fieldkit.config import get_fieldkit_data
from fieldkit.llm.log import get_db_path, get_legacy_db_path, get_read_db_path

log = logging.getLogger(__name__)

_DEFAULT_OPENCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
_DEVELOPER_PROVIDER = "openai"
_DEVELOPER_MODEL = "gpt-5.6-terra"


@dataclass(frozen=True)
class SpendCapDecision:
    """Whether measured fieldkit spend permits another unattended run."""

    allowed: bool
    reason_code: str
    detail: str
    cap_usd: float | None = None
    spend_usd: float | None = None


def _get_opencode_db_path() -> Path:
    return Path(os.environ.get("FIELDKIT_OPENCODE_DB", str(_DEFAULT_OPENCODE_DB)))


def reserve_run_db_path(issue_number: int) -> Path:
    """Reserve a unique durable database path for one driver execution."""
    root = (get_fieldkit_data() / "driver" / "llm-runs").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root / f"issue-{issue_number}-{uuid.uuid4().hex}.db"


def cap_safe_max_concurrent(configured: int, *, dry_run: bool, raw_cap: str | None) -> int:
    """Limit a cost-capped tick to one issue admitted against its spend snapshot."""
    if not dry_run and raw_cap is not None:
        return 1
    return configured


def _summary_db_path(db_path: Path | None) -> Path:
    """Resolve the ordinary read path unless the caller pinned a run database."""
    if db_path is not None:
        return db_path
    return get_read_db_path()


def get_spend_summary(issue_number: int, db_path: Path | None = None) -> str:
    """Return a one-line LLM spend summary for *issue_number*.

    Returns an empty string if the DB is missing or no rows match.

    Example output::

        LLM spend: $0.12 (1 234 input + 456 output tokens, 3 calls)

    Args:
        issue_number: GitHub issue number the driver is executing.
    """
    tag = f"driver-issue-{issue_number}"
    db_path = _summary_db_path(db_path)
    if not db_path.exists():
        log.debug("llm-calls.db not found — no spend summary")
        return ""

    try:
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(cost_usd), 0.0)   AS total_cost,
                    COALESCE(SUM(input_tokens), 0)  AS total_input,
                    COALESCE(SUM(output_tokens), 0) AS total_output,
                    COUNT(*)                         AS call_count
                FROM llm_calls
                WHERE account = ?
                """,
                (tag,),
            ).fetchone()
    except sqlite3.Error as exc:
        log.warning("spend query failed: %s", exc)
        return ""

    if row is None or row[3] == 0:
        return ""

    total_cost, total_input, total_output, call_count = row
    return f"LLM spend: ${total_cost:.2f} ({total_input:,} input + {total_output:,} output tokens, {call_count} calls)"


def _list_durable_run_dbs() -> list[Path] | None:
    """List retained run databases, preserving enumeration failures."""
    root = get_fieldkit_data() / "driver" / "llm-runs"
    if not root.exists():
        return []
    try:
        with os.scandir(root) as entries:
            return [Path(entry.path) for entry in entries if entry.name.endswith(".db") and entry.is_file()]
    except OSError as exc:
        log.warning("cannot enumerate durable LLM run databases at %s: %s", root, exc)
        return None


def _daily_spend_candidates() -> list[Path] | None:
    """Return every database that may contain driver spend."""
    candidates = [get_db_path()]
    if os.environ.get("FIELDKIT_LLM_LOG") is None:
        candidates.append(get_legacy_db_path())
    run_dbs = _list_durable_run_dbs()
    if run_dbs is None:
        return None
    candidates.extend(run_dbs)
    return list(dict.fromkeys(path.resolve() for path in candidates))


def _read_daily_spend(db_path: Path, today: str) -> float | None:
    """Read one database's spend, returning ``None`` when it is unreadable."""
    if not db_path.exists():
        return 0.0
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        # ts is TEXT holding an ISO-8601 UTC timestamp, so a lexicographic >= against
        # the bare date selects today's rows.
        row = conn.execute(
            """
        SELECT COALESCE(SUM(cost_usd), 0.0)
        FROM llm_calls
        WHERE account LIKE 'driver-issue-%' AND ts >= ?
        """,
            (today,),
        ).fetchone()
    except sqlite3.Error as exc:
        log.warning("daily spend query failed for %s, cannot verify the cap: %s", db_path, exc)
        return None
    finally:
        if conn is not None:
            conn.close()
    return 0.0 if row is None else float(row[0])


def get_daily_spend_total() -> float | None:
    """Return total ``cost_usd`` for all ``driver-issue-*`` runs today (UTC).

    Returns ``None`` — not 0.0 — when the total cannot be determined, so the caller can
    tell "nothing spent yet" apart from "cannot see what was spent". A cost guard that
    reads an unreadable database as $0 disables itself in exactly the circumstances it
    exists for, and ``run_driver()`` already fails closed on an unreadable busy set.

    A missing database is a genuine zero: the driver has never logged a call.
    """
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    candidates = _daily_spend_candidates()
    if candidates is None:
        return None
    total = 0.0
    for db_path in candidates:
        spend = _read_daily_spend(db_path, today)
        if spend is None:
            return None
        total += spend
    return total


def get_daily_developer_spend_total() -> float | None:
    """Return today's approved developer-automation spend for this checkout.

    The developer schedules themselves use OpenChamber's OpenAI provider, so their
    cost is recorded in OpenChamber's session database rather than ``llm-calls.db``.
    OpenChamber attaches sessions to the configured project, not to ephemeral
    worktree paths. The calling directory is therefore resolved through its project
    row before matching OpenAI Terra session costs are summed. Other interactive
    or provider-routed sessions must not consume this schedule-specific budget. A
    missing database or project is not evidence of zero spend: unattended admission
    must stop until accounting works. A readable project with no matching session
    today is a verifiable zero, permitting the first bounded run of the day.
    """
    db_path = _get_opencode_db_path()
    if not db_path.exists():
        log.warning("OpenChamber session database is missing; cannot verify developer spend")
        return None

    start_of_today = int(datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        project = conn.execute("SELECT id FROM project WHERE worktree = ?", (str(Path.cwd()),)).fetchone()
        if project is None:
            log.warning("OpenChamber has no project record for %s; cannot verify developer spend", Path.cwd())
            return None
        row = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(cost), 0.0)
            FROM session
            WHERE project_id = ? AND time_created >= ?
              AND json_extract(model, '$.providerID') = ?
              AND json_extract(model, '$.id') = ?
            """,
            (project[0], start_of_today, _DEVELOPER_PROVIDER, _DEVELOPER_MODEL),
        ).fetchone()
    except (OSError, sqlite3.Error) as exc:
        log.warning("developer spend query failed, cannot verify the cap: %s", exc)
        return None
    finally:
        if conn is not None:
            conn.close()
    if row is None or row[0] == 0:
        return 0.0
    return float(row[1])


def _evaluate_daily_spend_cap(
    raw_cap: str | None,
    *,
    cap_name: str,
    measurement_name: str,
    required: bool,
    spend_reader: Callable[[], float | None],
) -> SpendCapDecision:
    """Evaluate one daily cap against its dedicated accounting source."""
    if raw_cap is None:
        if required:
            return SpendCapDecision(False, "spend-cap-unset", f"{cap_name} is required")
        return SpendCapDecision(True, "spend-cap-not-configured", "no daily spend cap configured")
    try:
        cap = float(raw_cap)
    except ValueError:
        return SpendCapDecision(False, "spend-cap-invalid", f"{cap_name}={raw_cap!r} is not a finite number")
    if cap < 0 or not isfinite(cap):
        return SpendCapDecision(False, "spend-cap-invalid", f"{cap_name}={raw_cap!r} must be finite and non-negative")
    spend = spend_reader()
    if spend is None:
        return SpendCapDecision(
            False, "spend-unreadable", f"today's measured {measurement_name} spend could not be read", cap
        )
    if spend >= cap:
        return SpendCapDecision(
            False,
            "spend-cap-reached",
            f"today's measured {measurement_name} spend ${spend:.2f} has reached ${cap:.2f}",
            cap,
            spend,
        )
    return SpendCapDecision(
        True, "spend-within-cap", f"measured {measurement_name} spend is below the daily cap", cap, spend
    )


def evaluate_daily_spend_cap(raw_cap: str | None, *, required: bool) -> SpendCapDecision:
    """Evaluate the daily driver-loop LLM cap."""
    return _evaluate_daily_spend_cap(
        raw_cap,
        cap_name="FIELDKIT_DRIVER_SPEND_CAP",
        measurement_name="driver-issue",
        required=required,
        spend_reader=get_daily_spend_total,
    )


def evaluate_daily_developer_spend_cap(raw_cap: str | None, *, required: bool) -> SpendCapDecision:
    """Evaluate the daily OpenChamber developer-session cap."""
    return _evaluate_daily_spend_cap(
        raw_cap,
        cap_name="FIELDKIT_DEVELOPER_SPEND_CAP",
        measurement_name="OpenChamber developer-session",
        required=required,
        spend_reader=get_daily_developer_spend_total,
    )
