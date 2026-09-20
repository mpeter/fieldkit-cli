"""Tests for collect_champion_signals and _champion_signal_block coverage.

Targets:
- collect_champion_signals: no gmail.db, connect error, account filter,
  join-vs-sentinel, and the conn.close() finally guarantee.
- _champion_signal_block: invalid pursuit file, empty query output, no
  matching signal keywords, and the top-3 slice formatting.
"""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import fieldkit.commands.brief.collect as collect_mod
from fieldkit.commands.brief.collect import _champion_signal_block, collect_champion_signals
from fieldkit.gmail.exceptions import GmailDbNotFoundError

pytestmark = pytest.mark.unit


def _write_pursuit(path: Path, stage: str = "propose", extra_frontmatter: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nstage: {stage}\n{extra_frontmatter}---\n# Pursuit\n",
        encoding="utf-8",
    )


def _champion_pursuit_frontmatter() -> str:
    return (
        "meddpicc:\n"
        "  metrics: 1\n"
        "  economic-buyer: 1\n"
        "  decision-criteria: 1\n"
        "  decision-process: 1\n"
        "  identify-pain: 1\n"
        "  champion: 2\n"
        "  competition: 1\n"
        "  paper-process: 1\n"
    )


# ===========================================================================
# collect_champion_signals
# ===========================================================================


def test_collect_champion_signals_no_gmail_db_returns_sentinel_without_connecting(tmp_path: Path) -> None:
    """When gmail.db is unreachable, no db path lookup or connect is attempted."""
    with (
        patch.object(collect_mod, "_gmail_db_exists", return_value=False),
        patch.object(collect_mod, "get_gmail_db_path") as mock_get_path,
        patch.object(collect_mod, "_gmail_connect") as mock_connect,
    ):
        result = collect_champion_signals(tmp_path)

    assert result == "gmail.db not found — champion signals unavailable."
    mock_get_path.assert_not_called()
    mock_connect.assert_not_called()


def test_collect_champion_signals_connect_error_returns_sentinel(tmp_path: Path) -> None:
    """A GmailDbNotFoundError from _gmail_connect surfaces the unavailable sentinel."""
    with (
        patch.object(collect_mod, "_gmail_db_exists", return_value=True),
        patch.object(collect_mod, "get_gmail_db_path", return_value=tmp_path / "gmail.db"),
        patch.object(collect_mod, "_gmail_connect", side_effect=GmailDbNotFoundError("missing")),
    ):
        result = collect_champion_signals(tmp_path)

    assert result == "gmail.db unavailable — champion signals unavailable."


def test_collect_champion_signals_empty_blocks_returns_sentinel(tmp_path: Path) -> None:
    """When every pursuit yields no signal block, the sentinel is returned, not an empty join."""
    _write_pursuit(
        tmp_path / "accounts" / "acme-corp" / "pursuits" / "deal.md",
        extra_frontmatter=_champion_pursuit_frontmatter(),
    )
    mock_conn = MagicMock()

    with (
        patch.object(collect_mod, "_gmail_db_exists", return_value=True),
        patch.object(collect_mod, "get_gmail_db_path", return_value=tmp_path / "gmail.db"),
        patch.object(collect_mod, "_gmail_connect", return_value=mock_conn),
        patch.object(collect_mod, "_champion_signal_block", return_value=None),
    ):
        result = collect_champion_signals(tmp_path)

    assert result == "No champion signal data available."
    mock_conn.close.assert_called_once()


def test_collect_champion_signals_joins_non_empty_blocks(tmp_path: Path) -> None:
    """Non-None signal blocks are joined with a blank line between them."""
    _write_pursuit(tmp_path / "accounts" / "acme-corp" / "pursuits" / "deal-a.md")
    _write_pursuit(tmp_path / "accounts" / "acme-corp" / "pursuits" / "deal-b.md")
    mock_conn = MagicMock()

    def fake_block(conn: object, data_root: Path, account: str, path: Path) -> str | None:
        return f"block-{path.stem}" if path.stem == "deal-a" else None

    with (
        patch.object(collect_mod, "_gmail_db_exists", return_value=True),
        patch.object(collect_mod, "get_gmail_db_path", return_value=tmp_path / "gmail.db"),
        patch.object(collect_mod, "_gmail_connect", return_value=mock_conn),
        patch.object(collect_mod, "_champion_signal_block", side_effect=fake_block),
    ):
        result = collect_champion_signals(tmp_path)

    assert result == "block-deal-a"


def test_collect_champion_signals_account_filter_skips_other_accounts(tmp_path: Path) -> None:
    """account_filter prevents _champion_signal_block from even being called for other accounts."""
    _write_pursuit(tmp_path / "accounts" / "acme-corp" / "pursuits" / "deal.md")
    _write_pursuit(tmp_path / "accounts" / "globalpay" / "pursuits" / "deal.md")
    mock_conn = MagicMock()
    called_accounts: list[str] = []

    def fake_block(conn: object, data_root: Path, account: str, path: Path) -> str | None:
        called_accounts.append(account)
        return None

    with (
        patch.object(collect_mod, "_gmail_db_exists", return_value=True),
        patch.object(collect_mod, "get_gmail_db_path", return_value=tmp_path / "gmail.db"),
        patch.object(collect_mod, "_gmail_connect", return_value=mock_conn),
        patch.object(collect_mod, "_champion_signal_block", side_effect=fake_block),
    ):
        collect_champion_signals(tmp_path, account_filter="acme-corp")

    assert called_accounts == ["acme-corp"]


def test_collect_champion_signals_closes_connection_even_when_block_raises(tmp_path: Path) -> None:
    """The finally block closes the connection even if a per-pursuit block raises."""
    _write_pursuit(tmp_path / "accounts" / "acme-corp" / "pursuits" / "deal.md")
    mock_conn = MagicMock()

    with (
        patch.object(collect_mod, "_gmail_db_exists", return_value=True),
        patch.object(collect_mod, "get_gmail_db_path", return_value=tmp_path / "gmail.db"),
        patch.object(collect_mod, "_gmail_connect", return_value=mock_conn),
        patch.object(collect_mod, "_champion_signal_block", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError, match="boom"),
    ):
        collect_champion_signals(tmp_path)

    mock_conn.close.assert_called_once()


# ===========================================================================
# _champion_signal_block
# ===========================================================================


def test_champion_signal_block_invalid_pursuit_file_returns_none(tmp_path: Path) -> None:
    """A pursuit file with unparseable frontmatter is skipped, not raised."""
    pursuit = tmp_path / "accounts" / "acme-corp" / "pursuits" / "bad.md"
    pursuit.parent.mkdir(parents=True, exist_ok=True)
    pursuit.write_text("---\n: invalid: yaml: :\n---\n# Body\n", encoding="utf-8")

    conn = sqlite3.connect(":memory:")
    try:
        result = _champion_signal_block(conn, tmp_path, "acme-corp", pursuit)
    finally:
        conn.close()

    assert result is None


def test_champion_signal_block_empty_query_output_returns_none(tmp_path: Path) -> None:
    """A blank (whitespace-only) query result is treated as no signal data."""
    pursuit = tmp_path / "accounts" / "acme-corp" / "pursuits" / "deal.md"
    _write_pursuit(pursuit, extra_frontmatter=_champion_pursuit_frontmatter())
    account_dir = tmp_path / "accounts" / "acme-corp"
    (account_dir / "account.md").write_text("**Champion:** Jane Doe\n", encoding="utf-8")

    conn = sqlite3.connect(":memory:")
    try:
        with patch.object(collect_mod, "query_champion_signals", return_value="   \n  "):
            result = _champion_signal_block(conn, tmp_path, "acme-corp", pursuit)
    finally:
        conn.close()

    assert result is None


def test_champion_signal_block_no_matching_keyword_lines_returns_none(tmp_path: Path) -> None:
    """Output with no recognized signal keywords yields no block, even if non-empty."""
    pursuit = tmp_path / "accounts" / "acme-corp" / "pursuits" / "deal.md"
    _write_pursuit(pursuit, extra_frontmatter=_champion_pursuit_frontmatter())
    account_dir = tmp_path / "accounts" / "acme-corp"
    (account_dir / "account.md").write_text("**Champion:** Jane Doe\n", encoding="utf-8")

    conn = sqlite3.connect(":memory:")
    try:
        with patch.object(
            collect_mod,
            "query_champion_signals",
            return_value="Unrelated header\nSome other stat: 5\n",
        ):
            result = _champion_signal_block(conn, tmp_path, "acme-corp", pursuit)
    finally:
        conn.close()

    assert result is None


def test_champion_signal_block_formats_header_and_caps_at_three_lines(tmp_path: Path) -> None:
    """Matching lines are capped at 3 and prefixed under the account/pursuit/champion header."""
    pursuit = tmp_path / "accounts" / "acme-corp" / "pursuits" / "deal.md"
    _write_pursuit(pursuit, extra_frontmatter=_champion_pursuit_frontmatter())
    account_dir = tmp_path / "accounts" / "acme-corp"
    (account_dir / "account.md").write_text("**Champion:** Jane Doe\n", encoding="utf-8")

    output = (
        "Threads initiated: 3\n"
        "Last outbound: 2026-06-01\n"
        "Signal: replied fast\n"
        "Signal: fourth line should be dropped\n"
        "Unrelated noise line\n"
    )

    conn = sqlite3.connect(":memory:")
    try:
        with patch.object(collect_mod, "query_champion_signals", return_value=output):
            result = _champion_signal_block(conn, tmp_path, "acme-corp", pursuit)
    finally:
        conn.close()

    assert result is not None
    assert result.startswith("**acme-corp / deal** — champion: Jane")
    assert "Threads initiated: 3" in result
    assert "Last outbound: 2026-06-01" in result
    assert "Signal: replied fast" in result
    assert "fourth line should be dropped" not in result
    assert "Unrelated noise line" not in result
    assert len(result.splitlines()) == 4  # header + 3 capped signal lines
