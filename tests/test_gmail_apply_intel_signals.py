"""Tests for apply_intel._render_champion_candidates and apply_intel.build_signals.

Targets CRAP hotspots: _render_champion_candidates (cc=6) and build_signals (cc=4).
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.commands.gmail import apply_intel as ai

pytestmark = pytest.mark.unit


def _contact(email: str, name: str = "", threads: int = 3, msgs: int = 10, last_epoch: int | None = 1_000_000) -> tuple:
    return (email, name, threads, msgs, last_epoch)


def _champ(signal: str = "INITIATOR", threads: int = 10, initiated: int = 4, rate: float = 0.4, sent: int = 8) -> dict:
    return {"signal": signal, "threads": threads, "initiated": initiated, "rate": rate, "sent": sent}


# ---------------------------------------------------------------------------
# _render_champion_candidates
# ---------------------------------------------------------------------------


def test_render_champion_candidates_empty_contacts_shows_fallback() -> None:
    result = ai._render_champion_candidates([], {})
    assert any("No strong initiator signal in top contacts" in line for line in result)


def test_render_champion_candidates_header_present() -> None:
    result = ai._render_champion_candidates([], {})
    assert any("### Champion Candidates" in line for line in result)


def test_render_champion_candidates_no_champ_signal_excluded() -> None:
    contacts = [_contact("eve@acme-corp.com")]
    result = ai._render_champion_candidates(contacts, {})
    assert not any("eve@acme-corp.com" in line for line in result)
    assert any("No strong initiator signal in top contacts" in line for line in result)


def test_render_champion_candidates_reactive_signal_excluded() -> None:
    contacts = [_contact("alice@acme-corp.com")]
    champ_signals = {"alice@acme-corp.com": _champ(signal="REACTIVE", sent=10)}
    result = ai._render_champion_candidates(contacts, champ_signals)
    assert not any("alice@acme-corp.com" in line for line in result)
    assert any("No strong initiator signal in top contacts" in line for line in result)


def test_render_champion_candidates_initiator_with_sent_5_included() -> None:
    contacts = [_contact("bob@acme-corp.com")]
    champ_signals = {"bob@acme-corp.com": _champ(signal="INITIATOR", sent=5)}
    result = ai._render_champion_candidates(contacts, champ_signals)
    assert any("bob@acme-corp.com" in line for line in result)
    assert not any("No strong initiator signal in top contacts" in line for line in result)


def test_render_champion_candidates_initiator_with_sent_4_excluded() -> None:
    contacts = [_contact("carol@acme-corp.com")]
    champ_signals = {"carol@acme-corp.com": _champ(signal="INITIATOR", sent=4)}
    result = ai._render_champion_candidates(contacts, champ_signals)
    assert not any("carol@acme-corp.com" in line for line in result)
    assert any("No strong initiator signal in top contacts" in line for line in result)


def test_render_champion_candidates_mixed_signal_included() -> None:
    contacts = [_contact("dave@acme-corp.com")]
    champ_signals = {"dave@acme-corp.com": _champ(signal="MIXED", sent=6)}
    result = ai._render_champion_candidates(contacts, champ_signals)
    entry = next(line for line in result if "dave@acme-corp.com" in line)
    assert "MIXED" in entry


def test_render_champion_candidates_format_includes_rate_and_counts() -> None:
    contacts = [_contact("frank@acme-corp.com", name="Frank Lee")]
    champ_signals = {"frank@acme-corp.com": _champ(signal="INITIATOR", threads=10, initiated=4, rate=0.4, sent=8)}
    result = ai._render_champion_candidates(contacts, champ_signals)
    entry = next(line for line in result if "frank@acme-corp.com" in line)
    assert "**Frank Lee**" in entry
    assert "4/10 threads initiated" in entry
    assert "(40%)" in entry
    assert "8 sent" in entry
    assert "INITIATOR" in entry


def test_render_champion_candidates_display_name_falls_back_to_username() -> None:
    contacts = [_contact("grace@acme-corp.com", name="")]
    champ_signals = {"grace@acme-corp.com": _champ(sent=6)}
    result = ai._render_champion_candidates(contacts, champ_signals)
    entry = next(line for line in result if "grace@acme-corp.com" in line)
    assert "**grace**" in entry


def test_render_champion_candidates_caps_at_20_and_preserves_order() -> None:
    contacts = [_contact(f"user{i}@acme-corp.com") for i in range(25)]
    champ_signals = {f"user{i}@acme-corp.com": _champ(sent=10) for i in range(25)}
    result = ai._render_champion_candidates(contacts, champ_signals)
    contact_lines = [line for line in result if line.startswith("- **")]
    assert len(contact_lines) == 20
    assert "user0@acme-corp.com" in contact_lines[0]
    assert "user19@acme-corp.com" in contact_lines[19]
    assert not any("user20@acme-corp.com" in line for line in result)


# ---------------------------------------------------------------------------
# build_signals
# ---------------------------------------------------------------------------


def _write_pursuit(tmp_path: Path, text: str, filename: str = "acme-pursuit.md") -> Path:
    path = tmp_path / filename
    path.write_text(text, encoding="utf-8")
    return path


def test_build_signals_gmail_intel_stem_returns_none_without_reading_contacts(tmp_path: Path) -> None:
    path = _write_pursuit(tmp_path, "ignored content", filename="gmail-intel.md")
    with patch("fieldkit.commands.gmail.apply_intel.get_pursuit_contacts") as mock_contacts:
        result = ai.build_signals(path, "acme", db=MagicMock())
    assert result is None
    mock_contacts.assert_not_called()


def test_build_signals_no_contacts_returns_none(tmp_path: Path) -> None:
    path = _write_pursuit(tmp_path, "Nothing relevant here.")
    with (
        patch("fieldkit.commands.gmail.apply_intel.get_pursuit_contacts", return_value=[]),
        patch("fieldkit.commands.gmail.apply_intel.batch_champion_signals") as mock_batch,
        patch("fieldkit.commands.gmail.apply_intel.get_internal_domains", return_value=[]),
    ):
        result = ai.build_signals(path, "acme", db=MagicMock())
    assert result is None
    mock_batch.assert_not_called()


def test_build_signals_full_pipeline_returns_all_sections_in_order(tmp_path: Path) -> None:
    path = _write_pursuit(tmp_path, "Met with John Smith about renewal.")
    contacts = [_contact("jane@acme-corp.com", name="Jane Roe", msgs=10)]
    with (
        patch("fieldkit.commands.gmail.apply_intel.get_pursuit_contacts", return_value=contacts),
        patch("fieldkit.commands.gmail.apply_intel.batch_champion_signals", return_value={}),
        patch("fieldkit.commands.gmail.apply_intel.get_internal_domains", return_value=["acme-corp.com"]),
    ):
        result = ai.build_signals(path, "acme", db=MagicMock())
    assert result is not None
    assert result.index("## Gmail Signals") < result.index("### Top Account Contacts")
    assert result.index("### Top Account Contacts") < result.index("### Champion Candidates")
    assert result.index("### Champion Candidates") < result.index("### Contact Blindspots")


def test_build_signals_excludes_internal_domain_emails_from_pursuit_emails(tmp_path: Path) -> None:
    text = "Contact john@acme-corp.com and internal@example.com about this."
    path = _write_pursuit(tmp_path, text)
    with (
        patch("fieldkit.commands.gmail.apply_intel.get_pursuit_contacts", return_value=[]) as mock_contacts,
        patch("fieldkit.commands.gmail.apply_intel.get_internal_domains", return_value=["example.com"]),
    ):
        ai.build_signals(path, "acme", db=MagicMock())
    pursuit_emails = mock_contacts.call_args[0][2]
    assert "john@acme-corp.com" in pursuit_emails
    assert "internal@example.com" not in pursuit_emails


def test_build_signals_contact_emails_capped_at_25_for_champion_signals(tmp_path: Path) -> None:
    path = _write_pursuit(tmp_path, "No known contacts here.")
    contacts = [_contact(f"user{i}@acme-corp.com") for i in range(30)]
    with (
        patch("fieldkit.commands.gmail.apply_intel.get_pursuit_contacts", return_value=contacts),
        patch("fieldkit.commands.gmail.apply_intel.batch_champion_signals", return_value={}) as mock_batch,
        patch("fieldkit.commands.gmail.apply_intel.get_internal_domains", return_value=[]),
    ):
        ai.build_signals(path, "acme", db=MagicMock())
    contact_emails = mock_batch.call_args[0][1]
    assert len(contact_emails) == 25


def test_build_signals_header_includes_generated_date(tmp_path: Path) -> None:
    path = _write_pursuit(tmp_path, "No known contacts here.")
    contacts = [_contact("jane@acme-corp.com", name="Jane Roe", msgs=10)]
    fixed_now = datetime(2026, 3, 15, 12, 0, 0)
    with (
        patch("fieldkit.commands.gmail.apply_intel.get_pursuit_contacts", return_value=contacts),
        patch("fieldkit.commands.gmail.apply_intel.batch_champion_signals", return_value={}),
        patch("fieldkit.commands.gmail.apply_intel.get_internal_domains", return_value=[]),
        patch("fieldkit.commands.gmail.apply_intel.datetime") as mock_datetime,
    ):
        mock_datetime.now.return_value = fixed_now
        result = ai.build_signals(path, "acme", db=MagicMock())
    assert result is not None
    assert "_Auto-generated 2026-03-15 from email data since 2025-10-01_" in result


def test_build_signals_known_contact_gets_in_file_marker(tmp_path: Path) -> None:
    text = "Met with Jane Roe about renewal. Email jane@acme-corp.com for details."
    path = _write_pursuit(tmp_path, text)
    contacts = [_contact("jane@acme-corp.com", name="Jane Roe", msgs=10)]
    with (
        patch("fieldkit.commands.gmail.apply_intel.get_pursuit_contacts", return_value=contacts),
        patch("fieldkit.commands.gmail.apply_intel.batch_champion_signals", return_value={}),
        patch("fieldkit.commands.gmail.apply_intel.get_internal_domains", return_value=[]),
    ):
        result = ai.build_signals(path, "acme", db=MagicMock())
    assert result is not None
    assert "*(in file)*" in result
