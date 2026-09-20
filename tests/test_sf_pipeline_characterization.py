"""Characterization tests for the Salesforce frontmatter write path.

These tests lock in the CURRENT observable behavior of _run_sf_mode. They
assert what the code actually does — not what it ideally should do. If a future
refactor changes observable behavior, these tests catch it.
"""

import datetime
import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

from fieldkit.commands.sf.frontmatter import _run_sf_mode

pytestmark = pytest.mark.characterization

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# ── JSON payloads ─────────────────────────────────────────────────────────────

# Full payload: all SF_OPP_KEY_MAP fields populated
SF_SAMPLE_JSON = json.dumps(
    {
        "status": "ok",
        "stage": "Proposal/Pipeline",
        "close_date": "2026-06-30",
        "arr": "250000",
        "owner": "Test Owner",
        "next_steps": "Schedule follow-up",
        "pulled_at": "2026-05-16T12:00:00Z",
        "acv": "250000",
        "consulting_acv": "200000",
        "training_acv": "50000",
    }
)

# Minimal payload: only pulled_at set; all other fields absent → None → empty in file
SF_EMPTY_JSON = json.dumps(
    {
        "status": "ok",
        "pulled_at": "2026-05-16T12:00:00Z",
    }
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def simple_pursuit(tmp_path):
    """Isolated copy of pursuit_simple.md — minimal valid frontmatter."""
    dest = tmp_path / "pursuit_simple.md"
    shutil.copy(FIXTURES_DIR / "pursuit_simple.md", dest)
    return dest


@pytest.fixture
def full_pursuit(tmp_path):
    """Isolated copy of pursuit_full.md — historical local scores + empty sf_ fields."""
    dest = tmp_path / "pursuit_full.md"
    shutil.copy(FIXTURES_DIR / "pursuit_full.md", dest)
    return dest


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_frontmatter(path: Path) -> dict:
    """Extract and parse YAML frontmatter from a Markdown file."""
    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()
    dashes = [i for i, ln in enumerate(lines) if re.fullmatch(r"-{3,}\s*", ln)]
    fm_text = "\n".join(lines[dashes[0] + 1 : dashes[1]])
    return yaml.safe_load(fm_text) or {}


def _get_body(path: Path) -> str:
    """Return body text (everything after the closing frontmatter ---)."""
    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()
    dashes = [i for i, ln in enumerate(lines) if re.fullmatch(r"-{3,}\s*", ln)]
    return "\n".join(lines[dashes[1] + 1 :])


# ── Tests: _run_sf_mode ───────────────────────────────────────────────────────


def test_sf_mode_upserts_fields(simple_pursuit):
    """_run_sf_mode writes all SF_OPP_KEY_MAP fields from JSON into frontmatter."""
    _run_sf_mode(str(simple_pursuit), SF_SAMPLE_JSON)
    fm = _parse_frontmatter(simple_pursuit)

    assert fm["sf_stage"] == "Proposal/Pipeline"
    # Unquoted ISO date string → yaml parses as datetime.date (current behavior)
    assert fm["sf_close_date"] == datetime.date(2026, 6, 30)
    # Unquoted "250000" in YAML → integer (current behavior)
    assert fm["sf_arr"] == 250000
    assert fm["sf_owner"] == "Test Owner"
    assert fm["sf_next_steps"] == "Schedule follow-up"
    # Unquoted ISO datetime → yaml parses as datetime.datetime (current behavior)
    assert fm["sf_last_pulled"] == datetime.datetime(2026, 5, 16, 12, 0, tzinfo=datetime.UTC)
    assert fm["sf_acv"] == 250000
    assert fm["sf_consulting_acv"] == 200000
    assert fm["sf_training_acv"] == 50000


def test_sf_mode_preserves_existing_fields(full_pursuit):
    """_run_sf_mode preserves historical scores canonically and leaves transition history untouched."""
    _run_sf_mode(str(full_pursuit), SF_SAMPLE_JSON)
    fm = _parse_frontmatter(full_pursuit)

    legacy_meddpicc = fm["legacy_meddpicc"]
    assert legacy_meddpicc["schema_version"] == 1
    assert legacy_meddpicc["status"] == "historical"
    assert legacy_meddpicc["metrics"] == 2
    assert legacy_meddpicc["economic-buyer"] == 2
    assert legacy_meddpicc["decision-criteria"] == 2
    assert legacy_meddpicc["decision-process"] == 1
    assert legacy_meddpicc["identify-pain"] == 3
    assert legacy_meddpicc["champion"] == 2
    assert legacy_meddpicc["competition"] == 3
    assert legacy_meddpicc["paper-process"] == 1

    # Unquoted date string → yaml.safe_load returns datetime.date (current behavior)
    assert fm["last-transition"] == datetime.date(2026, 1, 15)
    history = fm["transition-history"]
    assert len(history) == 1
    assert history[0]["from"] == "validate"
    assert history[0]["to"] == "propose"


def test_sf_mode_empty_values(simple_pursuit):
    """_run_sf_mode writes sf_ keys even when JSON fields are absent (fixed: quoted empty string, not null)."""
    _run_sf_mode(str(simple_pursuit), SF_EMPTY_JSON)
    fm = _parse_frontmatter(simple_pursuit)

    # Keys must be present in frontmatter — not omitted
    for key in (
        "sf_stage",
        "sf_close_date",
        "sf_arr",
        "sf_owner",
        "sf_next_steps",
        "sf_acv",
        "sf_consulting_acv",
        "sf_training_acv",
    ):
        assert key in fm, f"Expected key {key!r} in frontmatter"
        # historic regression fixed: empty SF fields write a quoted empty string, not a bare null
        assert fm[key] == "", f"Expected '' for empty key {key!r}, got {fm[key]!r}"

    # pulled_at is always written; ISO datetime → yaml parses as datetime.datetime
    assert fm["sf_last_pulled"] == datetime.datetime(2026, 5, 16, 12, 0, tzinfo=datetime.UTC)


def test_sf_mode_empty_values_round_trip_as_empty_string(simple_pursuit):
    """historic regression regression: empty monetary fields write quoted '' and round-trip as '' not None.

    This test locks the fix at two layers:
    1. YAML text layer: the file must contain `key: ""` (quoted), not `key: ` (bare null).
    2. yaml.safe_load layer: parsing the file must return "" not None.

    A bare `key: ` emits None from yaml.safe_load. A quoted `key: ""` emits "".
    """
    _run_sf_mode(str(simple_pursuit), SF_EMPTY_JSON)

    # Layer 1: raw YAML text must contain the quoted form, not a bare key
    raw = simple_pursuit.read_text(encoding="utf-8")
    for key in ("sf_arr", "sf_acv", "sf_consulting_acv", "sf_training_acv"):
        assert f'{key}: ""' in raw, (
            f"historic regression: expected quoted empty string '{key}: \"\"' in file, "
            f"got bare key — _yaml_line fix may have been reverted"
        )

    # Layer 2: yaml.safe_load must return "" not None
    fm = _parse_frontmatter(simple_pursuit)
    for key in ("sf_arr", "sf_acv", "sf_consulting_acv", "sf_training_acv"):
        assert fm[key] == "", f"historic regression: expected '' for {key!r}, got {fm[key]!r}"

    # Non-monetary field also absent → also round-trips as ""
    assert fm["sf_owner"] == ""

    # sf_last_pulled is always written as a datetime — confirms non-empty fields unaffected
    assert isinstance(fm["sf_last_pulled"], datetime.datetime)


# ── Tests: round-trip body preservation ──────────────────────────────────────


def test_round_trip_preserves_body(simple_pursuit):
    """After _run_sf_mode, body content below the closing --- is unchanged."""
    body_before = _get_body(simple_pursuit)
    _run_sf_mode(str(simple_pursuit), SF_SAMPLE_JSON)
    body_after = _get_body(simple_pursuit)
    assert body_after == body_before
