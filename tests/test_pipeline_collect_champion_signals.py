"""Tests for collect_champion_signals in fieldkit.commands.pipeline.collect.

NOTE: this is a distinct function from collect_champion_signals in
commands/brief/collect.py (same name, different module — gaze-py's documented
same-name blind spot). Do not conflate the two.

Targets: gmail.db-missing early return, _gmail_connect failure, per-pursuit
continue guards (invalid frontmatter / closed stage / no-champion), the
extract_champion_name-or-account fallback, the happy-path result shape, and
the conn.close() finally guarantee.
"""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import fieldkit.commands.pipeline.collect as collect_mod
from fieldkit.commands.pipeline.collect import collect_champion_signals

pytestmark = pytest.mark.unit


def _write_pursuit(path: Path, stage: str = "propose", extra_frontmatter: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nstage: {stage}\n{extra_frontmatter}---\n# Pursuit\n",
        encoding="utf-8",
    )


def _champion_frontmatter(champion: int = 2) -> str:
    return (
        "meddpicc:\n"
        "  metrics: 1\n"
        "  economic-buyer: 1\n"
        "  decision-criteria: 1\n"
        "  decision-process: 1\n"
        "  identify-pain: 1\n"
        f"  champion: {champion}\n"
        "  competition: 1\n"
        "  paper-process: 1\n"
    )


# ===========================================================================
# 1. gmail.db missing → early return, _gmail_connect never called
# ===========================================================================


def test_no_gmail_db_returns_empty_without_connecting(tmp_path: Path) -> None:
    with (
        patch.object(collect_mod, "get_gmail_db_path", return_value=tmp_path / "missing.db"),
        patch.object(collect_mod, "_gmail_connect") as mock_connect,
    ):
        result = collect_champion_signals(tmp_path)

    assert result == []
    mock_connect.assert_not_called()


# ===========================================================================
# 2. _gmail_connect raises → returns [], loop body never runs
# ===========================================================================


def test_gmail_connect_error_returns_empty_and_skips_loop(tmp_path: Path) -> None:
    db_path = tmp_path / "gmail.db"
    db_path.touch()

    with (
        patch.object(collect_mod, "get_gmail_db_path", return_value=db_path),
        patch.object(collect_mod, "_gmail_connect", side_effect=sqlite3.Error("boom")),
        patch.object(collect_mod, "iterate_pursuits") as mock_iterate,
    ):
        result = collect_champion_signals(tmp_path)

    assert result == []
    mock_iterate.assert_not_called()


# ===========================================================================
# 3. Invalid pursuit frontmatter is skipped via continue; conn still closed
# ===========================================================================


def test_invalid_pursuit_frontmatter_skipped(tmp_path: Path) -> None:
    bad = tmp_path / "accounts" / "acme-corp" / "pursuits" / "bad.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("---\n: invalid: yaml: :\n---\n# Body\n", encoding="utf-8")

    db_path = tmp_path / "gmail.db"
    db_path.touch()
    mock_conn = MagicMock()

    with (
        patch.object(collect_mod, "get_gmail_db_path", return_value=db_path),
        patch.object(collect_mod, "_gmail_connect", return_value=mock_conn),
    ):
        result = collect_champion_signals(tmp_path)

    assert result == []
    mock_conn.close.assert_called_once()


# ===========================================================================
# 4. Closed-stage pursuit is skipped
# ===========================================================================


def test_closed_stage_pursuit_skipped(tmp_path: Path) -> None:
    _write_pursuit(
        tmp_path / "accounts" / "acme-corp" / "pursuits" / "deal.md",
        stage="closed-won",
        extra_frontmatter=_champion_frontmatter(),
    )
    db_path = tmp_path / "gmail.db"
    db_path.touch()
    mock_conn = MagicMock()

    with (
        patch.object(collect_mod, "get_gmail_db_path", return_value=db_path),
        patch.object(collect_mod, "_gmail_connect", return_value=mock_conn),
    ):
        result = collect_champion_signals(tmp_path)

    assert result == []


# ===========================================================================
# 5a. meddpicc is None → skipped
# ===========================================================================


def test_no_meddpicc_pursuit_skipped(tmp_path: Path) -> None:
    _write_pursuit(tmp_path / "accounts" / "acme-corp" / "pursuits" / "deal.md")
    db_path = tmp_path / "gmail.db"
    db_path.touch()
    mock_conn = MagicMock()

    with (
        patch.object(collect_mod, "get_gmail_db_path", return_value=db_path),
        patch.object(collect_mod, "_gmail_connect", return_value=mock_conn),
    ):
        result = collect_champion_signals(tmp_path)

    assert result == []


# ===========================================================================
# 5b. meddpicc.champion == 0 → skipped
# ===========================================================================


def test_champion_score_zero_pursuit_skipped(tmp_path: Path) -> None:
    _write_pursuit(
        tmp_path / "accounts" / "acme-corp" / "pursuits" / "deal.md",
        extra_frontmatter=_champion_frontmatter(champion=0),
    )
    db_path = tmp_path / "gmail.db"
    db_path.touch()
    mock_conn = MagicMock()

    with (
        patch.object(collect_mod, "get_gmail_db_path", return_value=db_path),
        patch.object(collect_mod, "_gmail_connect", return_value=mock_conn),
    ):
        result = collect_champion_signals(tmp_path)

    assert result == []


# ===========================================================================
# 6a. extract_champion_name returns a real name → used as-is
# ===========================================================================


def test_champion_name_used_when_extract_returns_name(tmp_path: Path) -> None:
    _write_pursuit(
        tmp_path / "accounts" / "acme-corp" / "pursuits" / "deal.md",
        extra_frontmatter=_champion_frontmatter(),
    )
    db_path = tmp_path / "gmail.db"
    db_path.touch()
    mock_conn = MagicMock()
    captured: dict[str, str] = {}

    def fake_query(conn: sqlite3.Connection, account: str) -> tuple[str, str, str]:
        captured["account"] = account
        return "3", "2026-01-01", "INITIATOR"

    with (
        patch.object(collect_mod, "get_gmail_db_path", return_value=db_path),
        patch.object(collect_mod, "_gmail_connect", return_value=mock_conn),
        patch.object(collect_mod, "extract_champion_name", return_value="Jane"),
        patch.object(collect_mod, "_query_champion_signals_inprocess", side_effect=fake_query),
    ):
        collect_champion_signals(tmp_path)

    assert captured["account"] == "Jane"


# ===========================================================================
# 6b. extract_champion_name returns "" → no identity-bound query
# ===========================================================================


def test_champion_name_falls_back_to_account_slug(tmp_path: Path) -> None:
    _write_pursuit(
        tmp_path / "accounts" / "acme-corp" / "pursuits" / "deal.md",
        extra_frontmatter=_champion_frontmatter(),
    )
    db_path = tmp_path / "gmail.db"
    db_path.touch()
    mock_conn = MagicMock()
    captured: dict[str, str] = {}

    def fake_query(conn: sqlite3.Connection, account: str) -> tuple[str, str, str]:
        captured["account"] = account
        return "3", "2026-01-01", "INITIATOR"

    with (
        patch.object(collect_mod, "get_gmail_db_path", return_value=db_path),
        patch.object(collect_mod, "_gmail_connect", return_value=mock_conn),
        patch.object(collect_mod, "extract_champion_name", return_value=""),
        patch.object(collect_mod, "_query_champion_signals_inprocess", side_effect=fake_query),
    ):
        collect_champion_signals(tmp_path)

    assert captured == {}


# ===========================================================================
# 7. Happy path — one dict with all six keys correctly populated
# ===========================================================================


def test_happy_path_produces_populated_signal_dict(tmp_path: Path) -> None:
    _write_pursuit(
        tmp_path / "accounts" / "acme-corp" / "pursuits" / "deal.md",
        extra_frontmatter=_champion_frontmatter(champion=2),
    )
    db_path = tmp_path / "gmail.db"
    db_path.touch()
    mock_conn = MagicMock()

    with (
        patch.object(collect_mod, "get_gmail_db_path", return_value=db_path),
        patch.object(collect_mod, "_gmail_connect", return_value=mock_conn),
        patch.object(collect_mod, "extract_champion_name", return_value="Jane"),
        patch.object(
            collect_mod,
            "_query_champion_signals_inprocess",
            return_value=("3", "2026-01-01", "INITIATOR"),
        ),
    ):
        result = collect_champion_signals(tmp_path)

    assert result == [
        {
            "account": "acme-corp",
            "deal": "deal",
            "champion": "Jane",
            "initiation": "3",
            "last_outbound": "2026-01-01",
            "signal": "INITIATOR",
        }
    ]


# ===========================================================================
# 8. conn.close() is called via finally, even in the normal multi-pursuit
#    case (call-count assertion), and even when the loop body raises.
# ===========================================================================


def test_conn_closed_once_across_multiple_pursuits(tmp_path: Path) -> None:
    _write_pursuit(
        tmp_path / "accounts" / "acme-corp" / "pursuits" / "deal-a.md",
        extra_frontmatter=_champion_frontmatter(),
    )
    _write_pursuit(
        tmp_path / "accounts" / "acme-corp" / "pursuits" / "deal-b.md",
        extra_frontmatter=_champion_frontmatter(),
    )
    db_path = tmp_path / "gmail.db"
    db_path.touch()
    mock_conn = MagicMock()

    with (
        patch.object(collect_mod, "get_gmail_db_path", return_value=db_path),
        patch.object(collect_mod, "_gmail_connect", return_value=mock_conn),
        patch.object(collect_mod, "extract_champion_name", return_value="Jane"),
        patch.object(
            collect_mod,
            "_query_champion_signals_inprocess",
            return_value=("", "", ""),
        ),
    ):
        result = collect_champion_signals(tmp_path)

    assert len(result) == 2
    mock_conn.close.assert_called_once()


def test_conn_closed_when_loop_body_raises_unexpectedly(tmp_path: Path) -> None:
    _write_pursuit(
        tmp_path / "accounts" / "acme-corp" / "pursuits" / "deal.md",
        extra_frontmatter=_champion_frontmatter(),
    )
    db_path = tmp_path / "gmail.db"
    db_path.touch()
    mock_conn = MagicMock()

    with (
        patch.object(collect_mod, "get_gmail_db_path", return_value=db_path),
        patch.object(collect_mod, "_gmail_connect", return_value=mock_conn),
        patch.object(collect_mod, "extract_champion_name", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError, match="boom"),
    ):
        collect_champion_signals(tmp_path)

    mock_conn.close.assert_called_once()
