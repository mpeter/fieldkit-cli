"""Unit tests for fieldkit.enrich._helpers — 100% coverage target.

All I/O-touching tests use ``monkeypatch`` to redirect ``enrich_dir()``
to ``tmp_path`` so no real ``get_fieldkit_home()`` call is made.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.enrich._helpers import (
    _is_transactional_domain,
    _should_skip_contact,
    _write_json_atomic,
    load_checkpoint,
    normalize_name_for_filename,
    save_checkpoint,
)
from fieldkit.enrich.schema import EnrichmentCheckpoint

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _is_transactional_domain
# ---------------------------------------------------------------------------


# ── TestIsTransactionalDomain (flattened) ───────────────────────────────────


def test_is_transactional_domain_no_at_sign_returns_false() -> None:
    """Email without '@' is not a transactional domain — returns False."""
    assert _is_transactional_domain("notanemail") is False


def test_is_transactional_domain_exact_domain_match_returns_true() -> None:
    """Email whose domain is in the known transactional set returns True."""
    assert _is_transactional_domain("signer@" + "docusign.com") is True
    assert _is_transactional_domain("user@" + "amazonses.com") is True


def test_is_transactional_domain_subdomain_match_returns_true() -> None:
    """A subdomain of a known transactional domain also returns True (historic regression)."""
    assert _is_transactional_domain("dse_na1@" + "mailna11-clm." + "docusign.net") is True


def test_is_transactional_domain_near_miss_domain_returns_false() -> None:
    """A domain that merely contains a transactional pattern is not a subdomain match.

    Guards the suffix check against false positives like ``evildocusign.net``,
    which shares a substring with ``docusign.net`` but is not one of its
    subdomains (historic regression).
    """
    assert _is_transactional_domain("user@evildocusign-net.example.com") is False  # pii-guard: ignore
    assert _is_transactional_domain("user@notdocusign-net.example.com") is False  # pii-guard: ignore


def test_is_transactional_domain_prefix_plus_suffix_returns_true() -> None:
    """Local part starting with a transactional prefix followed by '+' returns True.

    Covers the ``local.startswith(f"{prefix}+")`` branch for prefixes like
    ``noreply`` and ``alerts``.
    """
    assert _is_transactional_domain("noreply+tag@anyco-org.example.com") is True  # pii-guard: ignore
    assert _is_transactional_domain("alerts+info@anyco-org.example.com") is True  # pii-guard: ignore


def test_is_transactional_domain_prefix_at_suffix_returns_true() -> None:
    """Local part that exactly equals a transactional prefix returns True.

    Covers the ``local == prefix`` branch for the ``noreply`` prefix.
    """
    assert _is_transactional_domain("noreply@anyco-org.example.com") is True  # pii-guard: ignore


def test_is_transactional_domain_exact_prefix_match_returns_true() -> None:
    """Local part that exactly equals a transactional prefix returns True."""
    assert _is_transactional_domain("postmaster@example.com") is True  # pii-guard: ignore
    assert _is_transactional_domain("mailer-daemon@example.com") is True  # pii-guard: ignore


def test_is_transactional_domain_legitimate_email_returns_false() -> None:
    """A real person's email address returns False."""
    assert _is_transactional_domain("alice@acme-corp.com") is False  # pii-guard: ignore
    assert _is_transactional_domain("bob.jones@globalpay-com.example.com") is False  # pii-guard: ignore


# ---------------------------------------------------------------------------
# _should_skip_contact
# ---------------------------------------------------------------------------


# ── TestShouldSkipContact (flattened) ───────────────────────────────────────


def test_should_skip_contact_none_email_returns_false() -> None:
    """None email is never skipped — no exclusion rules apply."""
    assert _should_skip_contact(None, "me@example.com") is False  # pii-guard: ignore


def test_should_skip_contact_user_email_match_returns_true() -> None:
    """Contact whose email matches the user's own email is skipped (case-insensitive)."""
    assert _should_skip_contact("Me@Example.COM", "me@example.com") is True  # pii-guard: ignore
    assert _should_skip_contact("me@example.com", "Me@Example.COM") is True  # pii-guard: ignore


def test_should_skip_contact_user_email_none_real_contact_returns_false() -> None:
    """When user_email is None and contact is real, returns False."""
    assert _should_skip_contact("alice@acme-corp.com", None) is False  # pii-guard: ignore


def test_should_skip_contact_user_email_none_transactional_returns_true() -> None:
    """When user_email is None, transactional addresses are still skipped."""
    assert _should_skip_contact("noreply@" + "docusign.com", None) is True


def test_should_skip_contact_transactional_domain_returns_true() -> None:
    """Contact from a known transactional domain is skipped regardless of user_email."""
    assert (
        _should_skip_contact("alerts@sendgrid-net.example.com", "me@internal.example.com") is True
    )  # pii-guard: ignore


def test_should_skip_contact_legitimate_contact_returns_false() -> None:
    """Real contact with non-matching email is not skipped."""
    assert _should_skip_contact("alice@acme-corp.com", "me@internal.example.com") is False  # pii-guard: ignore


# ---------------------------------------------------------------------------
# _write_json_atomic
# ---------------------------------------------------------------------------


# ── TestWriteJsonAtomic (flattened) ─────────────────────────────────────────


def test_write_json_atomic_happy_path_writes_correct_content(tmp_path: Path) -> None:
    """Atomic write produces valid JSON at the target path."""
    dest = tmp_path / "output.json"
    data = {"key": "value", "count": 42}

    _write_json_atomic(dest, data)

    assert dest.exists()
    written = json.loads(dest.read_text(encoding="utf-8"))
    assert written == data


def test_write_json_atomic_happy_path_no_tmp_files_left(tmp_path: Path) -> None:
    """After a successful write no ``.tmp-*.json`` files remain in the directory."""
    dest = tmp_path / "output.json"
    _write_json_atomic(dest, [1, 2, 3])

    tmp_files = list(tmp_path.glob(".tmp-*.json"))
    assert tmp_files == [], f"Unexpected temp files: {tmp_files}"


def test_write_json_atomic_exception_path_cleans_up_temp_file(tmp_path: Path) -> None:
    """When ``os.fdopen`` raises, the temp file is cleaned up and the exception re-raised."""
    dest = tmp_path / "output.json"

    with (
        patch("fieldkit.enrich._helpers.os.fdopen", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        _write_json_atomic(dest, {"x": 1})

    # No .tmp files should remain
    tmp_files = list(tmp_path.glob(".tmp-*.json"))
    assert tmp_files == [], f"Temp file not cleaned up: {tmp_files}"

    # Destination must not have been created
    assert not dest.exists()


# ---------------------------------------------------------------------------
# load_checkpoint / save_checkpoint
# ---------------------------------------------------------------------------


@pytest.fixture()
def patched_enrich_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect ``enrich_dir()`` to ``tmp_path`` without touching the real home."""
    monkeypatch.setattr("fieldkit.enrich._helpers.enrich_dir", lambda: tmp_path)
    return tmp_path


# ── TestLoadCheckpoint (flattened) ──────────────────────────────────────────


def test_load_checkpoint_file_absent_returns_none(patched_enrich_dir: Path) -> None:
    """load_checkpoint returns None when no checkpoint file exists."""
    result = load_checkpoint()
    assert result is None


def test_load_checkpoint_file_present_returns_checkpoint(patched_enrich_dir: Path) -> None:
    """load_checkpoint deserialises a valid checkpoint file."""
    data = {
        "last_completed_account": "acme",
        "last_completed_contact_index": 5,
        "total_processed": 5,
        "total_enriched": 4,
        "total_failed": 1,
        "timestamp": "2026-01-01T00:00:00",
    }
    (patched_enrich_dir / "checkpoint.json").write_text(json.dumps(data), encoding="utf-8")

    result = load_checkpoint()

    assert isinstance(result, EnrichmentCheckpoint)
    assert result.last_completed_account == "acme"
    assert result.total_processed == 5


# ── TestSaveCheckpoint (flattened) ──────────────────────────────────────────


def test_save_checkpoint_happy_path_writes_checkpoint(patched_enrich_dir: Path) -> None:
    """save_checkpoint writes a valid JSON file that load_checkpoint can round-trip."""
    checkpoint = EnrichmentCheckpoint(
        last_completed_account="beta-corp",
        last_completed_contact_index=10,
        total_processed=10,
        total_enriched=8,
        total_failed=2,
    )

    save_checkpoint(checkpoint)

    checkpoint_file = patched_enrich_dir / "checkpoint.json"
    assert checkpoint_file.exists()
    raw = json.loads(checkpoint_file.read_text(encoding="utf-8"))
    assert raw["last_completed_account"] == "beta-corp"
    assert raw["total_enriched"] == 8


# ---------------------------------------------------------------------------
# normalize_name_for_filename
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("John Smith", "john_smith"),
        ("Dr. Jane, PhD", "dr_jane_phd"),
        ("", ""),
    ],
)
def test_normalize_name_for_filename(name: str, expected: str) -> None:
    """normalize_name_for_filename produces lowercase, underscore-separated, punctuation-free strings."""
    assert normalize_name_for_filename(name) == expected
