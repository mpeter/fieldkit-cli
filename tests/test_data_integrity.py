"""Tests for spec 042 — data integrity and silent loss prevention.

Covers:
- A1b: normalize_monetary() emits UserWarning for unparseable strings
- A2e: stage1_clean bypass caps stage2_extract confidence at 'low'
- A3d: RouteResult(accounts=[]) defaults to ['unknown']
- A4b: TransitionEntry mutual exclusion (Schema A + B raises ValueError)
"""

import json
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# A1b — normalize_monetary UserWarning
# ---------------------------------------------------------------------------


def test_normalize_monetary_warns_for_tbd() -> None:
    """normalize_monetary('TBD') emits UserWarning and returns None (not silently)."""

    from fieldkit.pursuit.models import PursuitFrontmatter

    with pytest.warns(UserWarning, match=r"TBD"):
        model = PursuitFrontmatter(stage="qualify", sf_arr="TBD")  # type: ignore[arg-type]
    assert model.sf_arr is None


def test_normalize_monetary_warns_for_placeholder_string() -> None:
    """normalize_monetary emits UserWarning for any non-numeric string placeholder."""

    from fieldkit.pursuit.models import PursuitFrontmatter

    with pytest.warns(UserWarning, match=r"N/A"):
        model = PursuitFrontmatter(stage="qualify", sf_acv="N/A")  # type: ignore[arg-type]
    assert model.sf_acv is None


def test_normalize_monetary_no_warning_for_valid_float() -> None:
    """normalize_monetary does NOT warn for valid numeric inputs."""
    import warnings

    from fieldkit.pursuit.models import PursuitFrontmatter

    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        model = PursuitFrontmatter(stage="qualify", sf_arr=500000.0)
    assert model.sf_arr == 500000.0


def test_normalize_monetary_no_warning_for_dollar_string() -> None:
    """normalize_monetary does NOT warn for parseable dollar-formatted strings."""
    import warnings

    from fieldkit.pursuit.models import PursuitFrontmatter

    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        model = PursuitFrontmatter(stage="qualify", sf_arr="$500,000")  # type: ignore[arg-type]
    assert model.sf_arr == 500000.0


def test_normalize_monetary_no_warning_for_none() -> None:
    """normalize_monetary does NOT warn for None (absent field)."""
    import warnings

    from fieldkit.pursuit.models import PursuitFrontmatter

    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        model = PursuitFrontmatter(stage="qualify", sf_arr=None)
    assert model.sf_arr is None


# ---------------------------------------------------------------------------
# A2e — stage1_clean bypass caps stage2_extract confidence at 'low'
# ---------------------------------------------------------------------------


def test_stage1_bypass_caps_confidence_at_low() -> None:
    """Stage 1 bypass (short transcript) must cap Stage 2 confidence at 'low'.

    synthesize() is mocked so this test is fully unit-isolated and does not
    require Vertex AI credentials (historic regression).
    """
    from fieldkit.ingest.pipeline import stage1_clean, stage2_extract

    # Feed a very short transcript that will trigger bypass (< _MIN_TRANSCRIPT_CHARS = 100)
    result = stage1_clean("short")
    assert result.bypassed is True

    # Mock the LLM returning 'high' — bypass must override it to 'low'
    payload = {"participants": [], "action_items": [], "key_decisions": [], "key_topics": [], "confidence": "high"}
    with patch("fieldkit.ingest.pipeline.synthesize", return_value=json.dumps(payload)):
        meta = stage2_extract(result)

    assert meta.confidence == "low"


def test_stage1_bypass_empty_transcript() -> None:
    """Empty transcript triggers bypass with bypassed=True."""
    from fieldkit.ingest.pipeline import stage1_clean

    result = stage1_clean("")
    assert result.bypassed is True
    assert result.text == ""


def test_stage1_no_bypass_for_long_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    """Long transcript (≥ 100 chars) does NOT trigger bypass."""
    monkeypatch.setenv("NO_LLM", "1")
    from fieldkit.ingest.pipeline import stage1_clean

    long_text = "Alice: Let's talk strategy.\nBob: Agreed on the roadmap.\n" * 5
    result = stage1_clean(long_text)
    assert result.bypassed is False


# ---------------------------------------------------------------------------
# A3d — RouteResult empty accounts defaults to ['unknown']
# ---------------------------------------------------------------------------


def test_route_result_empty_accounts_defaults_to_unknown() -> None:
    """RouteResult(accounts=[]) coerces to accounts=['unknown'] via __post_init__."""
    from fieldkit.ingest.router import Confidence, RouteResult

    route = RouteResult(accounts=[], confidence=Confidence.NONE, is_internal=False)
    assert route.accounts == ["unknown"]


def test_route_result_non_empty_accounts_preserved() -> None:
    """RouteResult with non-empty accounts list is not modified."""
    from fieldkit.ingest.router import Confidence, RouteResult

    route = RouteResult(accounts=["acme-corp"], confidence=Confidence.HIGH, is_internal=False)
    assert route.accounts == ["acme-corp"]


def test_primary_account_returns_first() -> None:
    """primary_account() returns the first account from a RouteResult."""
    from fieldkit.ingest.pipeline import primary_account
    from fieldkit.ingest.router import Confidence, RouteResult

    route = RouteResult(accounts=["acme-corp", "other"], confidence=Confidence.HIGH, is_internal=False)
    assert primary_account(route) == "acme-corp"


def test_primary_account_safe_on_empty_accounts() -> None:
    """primary_account() is safe even when accounts was originally empty (coerced to ['unknown'])."""
    from fieldkit.ingest.pipeline import primary_account
    from fieldkit.ingest.router import Confidence, RouteResult

    route = RouteResult(accounts=[], confidence=Confidence.NONE, is_internal=False)
    assert primary_account(route) == "unknown"


# ---------------------------------------------------------------------------
# A4b — TransitionEntry mutual exclusion
# ---------------------------------------------------------------------------


def test_transition_entry_mutual_exclusion() -> None:
    """TransitionEntry with both stage= and from_= raises ValidationError matching 'Schema A'."""
    from pydantic import ValidationError

    from fieldkit.pursuit.models import TransitionEntry

    with pytest.raises(ValidationError, match=r"Schema A"):
        TransitionEntry(stage="Proposal", **{"from": "Discovery"})


def test_transition_entry_schema_a_only_valid() -> None:
    """TransitionEntry with only stage= (Schema A) is valid."""
    from fieldkit.pursuit.models import TransitionEntry

    entry = TransitionEntry(stage="Proposal")
    assert entry.stage == "Proposal"
    assert entry.from_ is None
    assert entry.to is None


def test_transition_entry_schema_b_only_valid() -> None:
    """TransitionEntry with only from_=/to= (Schema B) is valid."""
    from fieldkit.pursuit.models import TransitionEntry

    entry = TransitionEntry(**{"from": "Discovery", "to": "Qualify"})
    assert entry.from_ == "Discovery"
    assert entry.to == "Qualify"
    assert entry.stage is None


def test_transition_entry_stage_and_to_raises() -> None:
    """TransitionEntry with stage= and to= (mixed schemas) raises ValidationError."""
    from pydantic import ValidationError

    from fieldkit.pursuit.models import TransitionEntry

    with pytest.raises(ValidationError, match=r"Schema A"):
        TransitionEntry(stage="Proposal", to="Qualify")


def test_transition_entry_all_none_valid() -> None:
    """TransitionEntry with all fields None is valid (historical entries may be sparse)."""
    from fieldkit.pursuit.models import TransitionEntry

    entry = TransitionEntry()
    assert entry.stage is None
    assert entry.from_ is None
    assert entry.to is None


# ---------------------------------------------------------------------------
# Additional edge-case coverage (review council additions)
# ---------------------------------------------------------------------------


def test_normalize_monetary_no_warning_for_empty_string() -> None:
    """normalize_monetary does NOT warn for empty string '' (SF sync writes '' for null)."""
    import warnings

    from fieldkit.pursuit.models import PursuitFrontmatter

    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        model = PursuitFrontmatter(stage="qualify", sf_arr="")  # type: ignore[arg-type]
    assert model.sf_arr is None


def test_stage2_extract_accepts_plain_str(monkeypatch: pytest.MonkeyPatch) -> None:
    """stage2_extract() accepts a plain str for backwards compat (notes_text fallback path)."""
    monkeypatch.setenv("NO_LLM", "1")
    from fieldkit.ingest.pipeline import TranscriptMeta, stage2_extract

    result = stage2_extract("some meeting notes text")
    assert isinstance(result, TranscriptMeta)
