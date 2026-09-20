"""Tests that get_gmail_db_path() caches config.yaml reads."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.gmail.discover import clear_gmail_caches, get_gmail_db_path


@pytest.fixture(autouse=True)
def _reset_cache() -> None:  # type: ignore[misc]
    """Ensure the cache is clean before and after each test."""
    clear_gmail_caches()
    yield  # type: ignore[misc]
    clear_gmail_caches()


def _make_config_mock(db_path: Path) -> MagicMock:
    """Return a mock CONFIG_PATH whose read_text returns a gmail_db yaml entry."""
    mock_config = MagicMock(spec=Path)
    mock_config.exists.return_value = True
    mock_config.read_text.return_value = f"gmail_db: {db_path}\n"
    return mock_config


@pytest.mark.unit
def test_get_gmail_db_path_reads_config_once(tmp_path: Path) -> None:
    """100 calls to get_gmail_db_path() should read config.yaml exactly once."""
    db_path = tmp_path / "custom_gmail.db"
    mock_config = _make_config_mock(db_path)

    with patch("fieldkit.config._loader.CONFIG_PATH", mock_config):
        results = [get_gmail_db_path() for _ in range(100)]

    # All 100 results should be identical
    assert all(r == results[0] for r in results)
    # Config file read_text should have been called exactly once (cached after that)
    assert mock_config.read_text.call_count == 1, f"Expected 1 read, got {mock_config.read_text.call_count}"


@pytest.mark.unit
def test_clear_gmail_caches_forces_reread(tmp_path: Path) -> None:
    """After clear_gmail_caches(), the next call re-reads config.yaml."""
    db_path = tmp_path / "custom_gmail.db"
    mock_config = _make_config_mock(db_path)

    with patch("fieldkit.config._loader.CONFIG_PATH", mock_config):
        get_gmail_db_path()
        assert mock_config.read_text.call_count == 1

        clear_gmail_caches()

        get_gmail_db_path()
        assert mock_config.read_text.call_count == 2, (
            f"Expected 2 reads after cache clear, got {mock_config.read_text.call_count}"
        )


@pytest.mark.unit
def test_get_gmail_db_path_fallback_no_config(tmp_path: Path) -> None:
    """When config.yaml doesn't exist, returns the default data-root path."""
    mock_config = MagicMock(spec=Path)
    mock_config.exists.return_value = False
    fake_data_root = tmp_path / "data-root"

    with (
        patch("fieldkit.config._loader.CONFIG_PATH", mock_config),
        patch("fieldkit.gmail.discover.get_fieldkit_data", return_value=fake_data_root),
    ):
        result = get_gmail_db_path()

    assert isinstance(result, Path)
    assert result == fake_data_root / "gmail.db"
    mock_config.read_text.assert_not_called()


# ---------------------------------------------------------------------------
# Task 11.2 — query_champion_signals and _build_addr_stats
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_query_champion_signals_returns_formatted_string(tmp_path: Path) -> None:
    """query_champion_signals returns a formatted string with expected keywords."""
    import sqlite3

    from fieldkit.commands.gmail.query import query_champion_signals

    # Build an in-memory DB with the required schema (must match fieldkit/gmail/names.py queries)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE people (
            email TEXT PRIMARY KEY,
            display_name TEXT,
            message_count INTEGER DEFAULT 0
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            thread_id TEXT,
            from_addr TEXT,
            to_addr TEXT,
            cc_addr TEXT,
            date_str TEXT,
            date_epoch INTEGER,
            subject TEXT,
            body_plain TEXT
        );
        CREATE TABLE threads (
            thread_id TEXT PRIMARY KEY,
            subject TEXT,
            message_count INTEGER,
            updated_at TEXT
        );
    """)
    # No matching person → returns "No people matched" string
    result = query_champion_signals(conn, "nobody@example.com")  # pii-guard: ignore
    assert isinstance(result, str)
    assert "No people matched" in result
    conn.close()


@pytest.mark.parametrize("bad_value", ["", "   "])
def test_get_gmail_db_path_raises_when_gmail_db_override_is_empty_or_whitespace(tmp_path: Path, bad_value: str) -> None:
    """Empty or whitespace gmail_db override raises ValueError."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"fieldkit_home: {tmp_path}\ngmail_db: {bad_value!r}\n",
        encoding="utf-8",
    )
    with (
        patch("fieldkit.config._loader.CONFIG_PATH", cfg),
        pytest.raises(ValueError, match="must not be empty or whitespace"),
    ):
        get_gmail_db_path()


@pytest.mark.unit
def test_build_addr_stats_returns_frequency_counts() -> None:
    """_build_addr_stats aggregates message counts per address."""
    from collections import namedtuple

    from fieldkit.commands.gmail.query import _build_addr_stats

    FakeRow = namedtuple("FakeRow", ["from_addr", "to_addr", "date_epoch"])

    rows = [
        FakeRow("alice@acme-corp.com", "bob@acme-corp.com", 1000),
        FakeRow("alice@acme-corp.com", "carol@acme-corp.com", 2000),
        FakeRow("bob@acme-corp.com", "alice@acme-corp.com", 3000),
    ]

    # Patch _is_noise to return False for all test addresses
    from unittest.mock import patch

    with patch("fieldkit.commands.gmail.query._is_noise", return_value=False):
        stats = _build_addr_stats(rows)  # type: ignore[arg-type]

    # alice appears as from_addr twice and to_addr once → msgs >= 2
    assert "alice@acme-corp.com" in stats
    assert stats["alice@acme-corp.com"]["msgs"] >= 2
    # last_epoch for alice should be the max epoch seen across all rows she appears in
    assert stats["alice@acme-corp.com"]["last_epoch"] == 3000
