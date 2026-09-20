"""Data collection functions for the pipeline review.

Iterates pursuit files and returns structured data for health rows,
champion signals, and blindspot analysis.
"""

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

import yaml
from pydantic import ValidationError

from fieldkit.gmail.discover import get_gmail_db_path
from fieldkit.gmail.query_domain import connect as _gmail_connect
from fieldkit.gmail.query_domain import query_champion_signals
from fieldkit.pursuit import (
    calculate_days_since,
    extract_champion_name,
    iterate_pursuits,
    read_accounts_config,
)
from fieldkit.pursuit.enums import Stage
from fieldkit.pursuit.io import load_pursuit
from fieldkit.pursuit.qualification import NativeQualificationStatus, native_qualification_status
from fieldkit.pursuit.stages import CLOSED_STAGES
from fieldkit.pursuit.stages import STAGE_ORDER as _STAGE_ORDER_LIST

# ── Stage ordering ────────────────────────────────────────────────────────────
# Derived from the canonical STAGE_ORDER list in fieldkit.pursuit.stages.
# Maps stage string → sort priority (lower = higher in pipeline = shown first).
STAGE_ORDER: dict[str, int] = {stage: i for i, stage in enumerate(reversed(_STAGE_ORDER_LIST))}

# Gate icon: overall gate status stored in frontmatter
GATE_ICONS: dict[str, str] = {
    "pass": "✅",
    "override": "⚠️",
    "fail": "❌",
    "pending": "🔲",
}

# CLOSED_STAGES imported from fieldkit.pursuit.stages — single source of truth.
# Now includes "won-lost" (legacy alias) — no local extension needed.

log = logging.getLogger(__name__)


# ── Data model ───────────────────────────────────────────────────────────────


@dataclass
class PursuitRow:
    """One row in the pipeline health table."""

    account: str
    deal: str
    stage: str
    gate_status: str
    days_in_stage: int  # -1 when unknown
    native_qualification: NativeQualificationStatus = "unavailable (no Salesforce opportunity link)"
    close_date_str: str = ""  # ISO date string from sf_close_date, or ""
    # Stage velocity: average days per stage transition (days_in_stage / history length).
    # None when transition_history is unavailable.
    velocity_days_per_stage: float | None = None


class ChampionSignal(TypedDict):
    """One active pursuit's derived Gmail champion signal."""

    account: str
    deal: str
    champion: str
    initiation: str
    last_outbound: str
    signal: str


# ── Private helpers ───────────────────────────────────────────────────────────


def _extract_account(parts: tuple[str, ...]) -> str:
    """Extract account name from path parts: ...accounts/<account>/pursuits/..."""
    try:
        accounts_idx = list(parts).index("accounts")
        return parts[accounts_idx + 1]
    except (ValueError, IndexError):
        return "unknown"


def _parse_champion_output(stdout: str) -> tuple[str, str, str]:
    """Parse champion query output into (initiation, last_outbound, signal).

    Matches the indented output format from query_champion_signals:
      '  Threads initiated   : 3  (50% initiation rate)'
      '  Last outbound       : 2026-01-15  30d ago'
      '  Signal: INITIATOR'
    """
    initiation = last_outbound = signal = ""
    for line in stdout.strip().splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith("threads initiated"):
            initiation = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
        elif low.startswith("last outbound"):
            last_outbound = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
        elif low.startswith("signal:"):
            signal = stripped.split(":", 1)[1].strip()
    return initiation, last_outbound, signal


def _query_champion_signals_inprocess(conn: sqlite3.Connection, account: str) -> tuple[str, str, str]:
    """Query champion signals in-process using a shared sqlite3 connection.

    Replaces the former subprocess approach (implementation change).  Calls
    ``query_champion_signals(conn, account)`` directly — same function used by
    morning_brief — and parses the returned string with ``_parse_champion_output``.

    Returns (initiation, last_outbound, signal); all empty strings on failure.
    """
    try:
        output = query_champion_signals(conn, account)
        if not output.strip():
            return "", "", ""
        # query_champion_signals returns "No people matched '…'" when the name
        # is not in the DB (exit-0 path in the old subprocess flow).
        if "Last outbound" not in output:
            return "", "", ""
        return _parse_champion_output(output)
    except (sqlite3.Error, OSError, ValueError):
        log.warning(
            "Champion signal query for %r failed — run 'fieldkit gmail sync' to populate cache",
            account,
            exc_info=True,
        )
    return "", "", ""


def _current_gate_status(fm: Any) -> str:
    """Keep an explicit override visible; otherwise no local score can clear the gate."""
    return "override" if getattr(fm, "gate_status", None) == "override" else "pending"


# ── Collectors ───────────────────────────────────────────────────────────────


def collect_pursuit_health(data_root: Path) -> list[PursuitRow]:
    """Iterate non-closed pursuits and return a list of PursuitRow."""
    rows: list[PursuitRow] = []

    for path in iterate_pursuits(data_root):
        try:
            fm, _, _ = load_pursuit(path)
        except (ValueError, yaml.YAMLError, ValidationError):
            continue  # skip invalid pursuit file

        stage = fm.stage
        if stage in CLOSED_STAGES:
            continue

        # Account and deal names from path: accounts/<account>/pursuits/<deal>.md
        parts = path.parts
        account = _extract_account(parts)
        deal = path.stem

        rows.append(_build_pursuit_row(fm, path, account, deal, stage))

    return rows


def collect_champion_signals(data_root: Path) -> list[ChampionSignal]:
    """Query Gmail for active pursuits that have a derived champion identity.

    Returns list of dicts with keys: account, deal, champion,
    initiation, last_outbound, signal. Missing data fields are empty strings.

    Uses a single shared sqlite3.Connection opened once per call (implementation change),
    matching the pattern established in morning_brief/main.py.
    """
    signals: list[ChampionSignal] = []

    db_path = get_gmail_db_path()
    if not db_path.exists():
        log.warning("gmail.db not found at %s — champion signals unavailable", db_path)
        return signals

    try:
        conn = _gmail_connect(db_path)
    except (sqlite3.Error, OSError):
        log.warning("Cannot open gmail.db for champion signals", exc_info=True)
        return signals

    try:
        for path in iterate_pursuits(data_root):
            try:
                fm, _, _ = load_pursuit(path)
            except (ValueError, yaml.YAMLError, ValidationError):
                continue

            if fm.stage in CLOSED_STAGES:
                continue
            account = _extract_account(path.parts)
            deal = path.stem
            account_dir = path.parent.parent  # pursuits/../ → account dir
            champion_name = extract_champion_name(account_dir)
            if not champion_name:
                continue
            initiation, last_outbound, signal = _query_champion_signals_inprocess(conn, champion_name)

            signals.append(
                {
                    "account": account,
                    "deal": deal,
                    "champion": champion_name,
                    "initiation": initiation,
                    "last_outbound": last_outbound,
                    "signal": signal,
                }
            )
    finally:
        conn.close()

    return signals


def _build_pursuit_row(fm: Any, path: Path, account: str, deal: str, stage: str) -> PursuitRow:
    """Build a PursuitRow from a loaded pursuit frontmatter model.

    Local gate passes/failures are not current native qualification evidence.
    Explicit overrides remain visible; every other local status is pending.
    """
    gate_status = _current_gate_status(fm)
    last_transition = fm.last_transition
    lt_str = str(last_transition) if last_transition else ""
    days_in_stage = calculate_days_since(lt_str)
    if fm.sf_close_date:
        close_date_str = str(fm.sf_close_date)
    elif stage != Stage.PRE_PIPELINE:
        close_date_str = "⚠ no date"  # historic regression: visible indicator for missing close date
    else:
        close_date_str = ""

    history = fm.transition_history
    if history is not None and days_in_stage >= 0:
        velocity_days_per_stage: float | None = days_in_stage / max(len(history), 1)
    else:
        velocity_days_per_stage = None

    return PursuitRow(
        account=account,
        deal=deal,
        stage=stage,
        gate_status=gate_status,
        days_in_stage=days_in_stage,
        native_qualification=native_qualification_status(getattr(fm, "sf_opportunity_id", None)),
        close_date_str=close_date_str,
        velocity_days_per_stage=velocity_days_per_stage,
    )


def _maybe_collect_champion_signal(
    path: Path,
    account: str,
    deal: str,
    gmail_conn: sqlite3.Connection | None,
) -> ChampionSignal | None:
    """Return a champion signal dict if applicable, else None."""
    if gmail_conn is None:
        return None
    account_dir = path.parent.parent
    champion_name = extract_champion_name(account_dir)
    if not champion_name:
        return None
    initiation, last_outbound, signal = _query_champion_signals_inprocess(gmail_conn, champion_name)
    return {
        "account": account,
        "deal": deal,
        "champion": champion_name,
        "initiation": initiation,
        "last_outbound": last_outbound,
        "signal": signal,
    }


def _build_blindspot_data(
    active_counts: dict[str, int],
    data_root: Path,
    account_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Compute blindspot_data from active_counts + accounts config."""
    try:
        config = read_accounts_config(data_root)
    except (FileNotFoundError, yaml.YAMLError):
        config = {}

    accounts_cfg = config.get("accounts", {})
    if not isinstance(accounts_cfg, dict):
        accounts_cfg = {}

    blindspot_data: list[dict[str, Any]] = []
    for acct_name, acct_data in accounts_cfg.items():
        if account_filter is not None and acct_name != account_filter:
            continue
        if not isinstance(acct_data, dict):
            continue
        if acct_data.get("internal", False):
            continue  # historic regression: skip internal accounts from blindspot check
        threshold = acct_data.get("blindspot_threshold", 1)
        active = active_counts.get(acct_name, 0)
        status = "ok" if active >= threshold else "blindspot"
        blindspot_data.append(
            {
                "account": acct_name,
                "active_pursuits": active,
                "threshold": threshold,
                "status": status,
            }
        )
    return blindspot_data


def collect_all_pursuit_data(
    data_root: Path,
    account_filter: str | None = None,
) -> tuple[list[PursuitRow], list[ChampionSignal] | None, list[dict[str, Any]]]:
    """Single-pass collector: iterates pursuits once and returns all three datasets.

    Args:
        data_root:      Workspace root path.
        account_filter: When set, restricts the scan to
                        ``accounts/<account_filter>/pursuits/``.

    Returns:
        (rows, champion_signals, blindspot_data) where:
        - rows: list of PursuitRow (same as collect_pursuit_health)
        - champion_signals: list of champion signal dicts, or **None** when
          gmail.db is unavailable (historic regression: callers should render a clear
          unavailability message rather than "no champions found")
        - blindspot_data: list of blindspot dicts (same as collect_blindspot_data)
    """
    rows: list[PursuitRow] = []
    signals: list[ChampionSignal] = []
    active_counts: dict[str, int] = {}

    # implementation change: open a single shared connection for all champion signal queries
    # instead of spawning one subprocess per pursuit.  Matches the pattern in
    # morning_brief/main.py.  conn is None when gmail.db is absent.
    db_path = get_gmail_db_path()
    gmail_conn: sqlite3.Connection | None = None
    gmail_db_available = db_path.exists()
    if gmail_db_available:
        try:
            gmail_conn = _gmail_connect(db_path)
        except (sqlite3.Error, OSError):
            log.warning("Cannot open gmail.db for champion signals", exc_info=True)
            gmail_db_available = False
    else:
        log.warning(
            "gmail.db not found at %s — champion signals unavailable. "  # historic regression
            "Run 'fieldkit gmail sync' to build the database.",
            db_path,
        )

    if account_filter is not None:
        pursuit_paths = sorted(
            (data_root / "accounts" / account_filter / "pursuits").glob("*.md"),
        )
        paths_iter = (p for p in pursuit_paths if ".template" not in str(p) and "gmail-intel" not in str(p))
    else:
        paths_iter = iterate_pursuits(data_root)

    try:
        for path in paths_iter:
            try:
                fm, _, _ = load_pursuit(path)
            except (ValueError, yaml.YAMLError, ValidationError):
                continue

            stage = fm.stage
            parts = path.parts
            account = _extract_account(parts)
            deal = path.stem

            if stage not in CLOSED_STAGES:
                rows.append(_build_pursuit_row(fm, path, account, deal, stage))
                active_counts[account] = active_counts.get(account, 0) + 1

                sig = _maybe_collect_champion_signal(path, account, deal, gmail_conn)
                if sig is not None:
                    signals.append(sig)
    finally:
        if gmail_conn is not None:
            gmail_conn.close()

    blindspot_data = _build_blindspot_data(active_counts, data_root, account_filter)
    # historic regression: return None for champion_signals when gmail.db was unavailable
    # so callers can render a clear "unavailable" message vs "no champions found".
    # Champion signals are filtered by champion name only (account-level).
    # Per-pursuit filtering by slug/thread is tracked in historic regression (issue #253).
    return rows, (signals if gmail_db_available else None), blindspot_data


def collect_blindspot_data(data_root: Path) -> list[dict[str, Any]]:
    """Read accounts config and report accounts with no active pursuits.

    Returns list of dicts with keys: account, active_pursuits, threshold, status.
    """
    try:
        config = read_accounts_config(data_root)
    except (FileNotFoundError, yaml.YAMLError):
        return []

    accounts_cfg = config.get("accounts", {})
    if not isinstance(accounts_cfg, dict):
        accounts_cfg = {}

    # Count active pursuits per account
    active_counts: dict[str, int] = {}
    for path in iterate_pursuits(data_root):
        try:
            fm, _, _ = load_pursuit(path)
        except (ValueError, yaml.YAMLError, ValidationError):
            continue  # skip invalid pursuit file
        if fm.stage in CLOSED_STAGES:
            continue
        parts = path.parts
        acct = _extract_account(parts)
        active_counts[acct] = active_counts.get(acct, 0) + 1

    result: list[dict[str, Any]] = []
    for acct_name, acct_data in accounts_cfg.items():
        if not isinstance(acct_data, dict):
            continue
        if acct_data.get("internal", False):
            continue  # skip accounts flagged internal: true in accounts.yaml
        threshold = acct_data.get("blindspot_threshold", 1)
        active = active_counts.get(acct_name, 0)
        status = "ok" if active >= threshold else "blindspot"
        result.append(
            {
                "account": acct_name,
                "active_pursuits": active,
                "threshold": threshold,
                "status": status,
            }
        )

    return result
