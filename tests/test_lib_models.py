"""Unit tests for lib.models — validates Pydantic models against fixtures and real pursuits."""

import datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from fieldkit.pursuit.models import AccountFrontmatter, LegacyMEDDPICC, PursuitFrontmatter
from tests.conftest import DATA_ROOT, needs_data

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
_DATA = DATA_ROOT or Path("/nonexistent")


def _load_frontmatter(path: Path) -> dict:
    """Extract YAML frontmatter from a markdown file."""
    text = path.read_text()
    parts = text.split("---", 2)
    return yaml.safe_load(parts[1])


# ── TestImportSmoke (flattened) ─────────────────────────────────────────────


def test_import_smoke_import_smoke():
    assert callable(PursuitFrontmatter)
    assert callable(LegacyMEDDPICC)
    assert callable(AccountFrontmatter)


# ── TestFixtureParsing (flattened) ──────────────────────────────────────────


def test_fixture_parsing_parse_pursuit_simple():
    data = _load_frontmatter(ROOT / "tests/fixtures/pursuit_simple.md")
    m = PursuitFrontmatter(**data)
    assert m.stage == "discover"
    assert m.legacy_meddpicc is not None
    assert m.legacy_meddpicc.status == "historical"
    assert m.sf_opportunity_id is None
    assert m.sf_stage is None


def test_fixture_parsing_parse_pursuit_full():
    data = _load_frontmatter(ROOT / "tests/fixtures/pursuit_full.md")
    m = PursuitFrontmatter(**data)
    assert m.stage == "propose"
    assert m.legacy_meddpicc is not None
    assert m.legacy_meddpicc.metrics == 2
    assert m.legacy_meddpicc.identify_pain == 3
    assert "composite" not in m.legacy_meddpicc.model_dump(exclude_unset=True)
    assert m.sf_opportunity_id == ""
    assert m.sf_stage == ""


def test_legacy_opportunity_territory_is_ignored_by_active_model() -> None:
    model = PursuitFrontmatter.model_validate({"stage": "discover", "sf_opportunity_territory": "LEGACY_TERRITORY"})

    assert "sf_opportunity_territory" not in PursuitFrontmatter.model_fields
    assert "sf_opportunity_territory" not in model.model_dump()


# ── TestRealPursuits (flattened) ────────────────────────────────────────────


@needs_data
@pytest.mark.integration  # requires real account files via DATA_ROOT
def test_real_pursuits_parse_real_acme_corp_pursuit():
    target = _DATA / "accounts/acme-corp/pursuits/ads-repave-services.md"  # pii-guard: ignore
    if not target.exists():
        pytest.skip("Real account file not available in this environment")
    data = _load_frontmatter(target)
    m = PursuitFrontmatter(**data)
    assert m.sf_stage is not None


@needs_data
@pytest.mark.integration  # requires real account files via DATA_ROOT
def test_real_pursuits_parse_real_globalpay_pursuit():
    import pytest

    target = _DATA / "accounts/globalpay/pursuits/add-on-services.md"  # pii-guard: ignore
    if not target.exists():
        pytest.skip("Real account file not available in this environment")
    data = _load_frontmatter(target)
    m = PursuitFrontmatter(**data)
    assert isinstance(m.sf_close_date, (str, datetime.date))
    assert m.sf_arr is None or isinstance(m.sf_arr, str)


# ── TestValidation (flattened) ──────────────────────────────────────────────


def test_validation_invalid_stage_rejected():
    with pytest.raises(ValidationError, match=r"stage"):
        PursuitFrontmatter(stage="invalid")


# ── TestAccountFrontmatter (flattened) ──────────────────────────────────────


def test_account_frontmatter_account_frontmatter_minimal():
    m = AccountFrontmatter(name="test")
    assert m.name == "test"
    assert m.domains is None


# ── historic regression: TransitionEntry model ───────────────────────────────────────────


# ── TestTransitionEntry (flattened) ─────────────────────────────────────────


def test_transition_entry_import():
    from fieldkit.pursuit.models import TransitionEntry

    assert callable(TransitionEntry)


def test_transition_entry_minimal_entry():
    from fieldkit.pursuit.models import TransitionEntry

    entry = TransitionEntry(**{"from": "discover", "to": "validate", "date": "2026-01-15"})
    assert entry.from_ == "discover"
    assert entry.to == "validate"
    assert entry.date == "2026-01-15"


def test_transition_entry_all_fields():
    from fieldkit.pursuit.models import TransitionEntry

    entry = TransitionEntry(
        **{
            "from": "propose",
            "to": "negotiate",
            "date": "2026-03-01",
            "gate-result": "pass",
            "override-reason": "exec approval",
        }
    )
    assert entry.gate_result == "pass"
    assert entry.override_reason == "exec approval"


def test_transition_entry_all_fields_optional():
    from fieldkit.pursuit.models import TransitionEntry

    # Empty entry is valid — all fields optional
    entry = TransitionEntry()
    assert entry.from_ is None
    assert entry.to is None
    assert entry.date is None


def test_transition_entry_extra_fields_ignored():
    from fieldkit.pursuit.models import TransitionEntry

    # extra="ignore" — unknown keys must not raise
    entry = TransitionEntry(**{"from": "discover", "unknown_key": "value"})
    assert entry.from_ == "discover"


def test_transition_entry_model_dump_by_alias():
    from fieldkit.pursuit.models import TransitionEntry

    entry = TransitionEntry(**{"from": "discover", "to": "validate", "gate-result": "pass"})
    dumped = entry.model_dump(by_alias=True, exclude_none=True)
    assert "from" in dumped
    assert "from_" not in dumped
    assert dumped["from"] == "discover"
    assert dumped["gate-result"] == "pass"


def test_transition_entry_pursuit_frontmatter_transition_history_typed():
    """PursuitFrontmatter.transition_history accepts TransitionEntry dicts."""
    from fieldkit.pursuit.models import PursuitFrontmatter, TransitionEntry

    fm = PursuitFrontmatter(
        **{
            "stage": "discover",
            "transition-history": [
                {"from": "pre-pipeline", "to": "discover", "date": "2026-01-01"},
            ],
        }
    )
    assert fm.transition_history is not None
    assert len(fm.transition_history) == 1
    assert isinstance(fm.transition_history[0], TransitionEntry)
    assert fm.transition_history[0].from_ == "pre-pipeline"


# ── historic regression: stage field round-trip ──────────────────────────────────────────


# ── TestTransitionEntryStageField (flattened) ───────────────────────────────


def test_transition_entry_stage_field_schema_a_entry_preserves_stage() -> None:
    """Schema A {stage, date} entry must survive model round-trip without data loss."""
    from fieldkit.pursuit.models import TransitionEntry

    entry = TransitionEntry(**{"stage": "discover", "date": "2026-01-15"})
    assert entry.stage == "discover"
    assert entry.date == "2026-01-15"
    assert entry.from_ is None
    assert entry.to is None


def test_transition_entry_stage_field_schema_a_model_dump_includes_stage() -> None:
    """model_dump(exclude_none=True) must include stage when set."""
    from fieldkit.pursuit.models import TransitionEntry

    entry = TransitionEntry(**{"stage": "validate", "date": "2026-03-10"})
    dumped = entry.model_dump(by_alias=True, exclude_none=True)
    assert "stage" in dumped
    assert dumped["stage"] == "validate"
    assert "from" not in dumped
    assert "to" not in dumped


def test_transition_entry_stage_field_schema_b_model_dump_omits_stage_when_none() -> None:
    """Schema B {from, to, date} must not emit a stage key when stage is None."""
    from fieldkit.pursuit.models import TransitionEntry

    entry = TransitionEntry(**{"from": "discover", "to": "validate", "date": "2026-02-01"})
    dumped = entry.model_dump(by_alias=True, exclude_none=True)
    assert "stage" not in dumped
    assert dumped["from"] == "discover"
    assert dumped["to"] == "validate"


def test_transition_entry_stage_field_render_transition_history_includes_stage_for_schema_a() -> None:
    """_render_transition_history must emit stage: for Schema A entries."""
    from fieldkit.pursuit.io import _render_transition_history
    from fieldkit.pursuit.models import TransitionEntry

    entry = TransitionEntry(**{"stage": "validate", "date": "2026-05-01"})
    lines = _render_transition_history([entry])
    combined = "\n".join(lines)
    assert "stage: validate" in combined
    assert "from:" not in combined


def test_transition_entry_stage_field_render_transition_history_omits_stage_for_schema_b() -> None:
    """_render_transition_history must NOT emit stage: for Schema B entries."""
    from fieldkit.pursuit.io import _render_transition_history
    from fieldkit.pursuit.models import TransitionEntry

    entry = TransitionEntry(**{"from": "discover", "to": "validate", "date": "2026-05-01"})
    lines = _render_transition_history([entry])
    combined = "\n".join(lines)
    assert "stage:" not in combined
    assert "from: discover" in combined
    assert "to: validate" in combined


# ---------------------------------------------------------------------------
# 4A.1 coerce_yaml_types (PursuitFrontmatter model_validator)
# ---------------------------------------------------------------------------


# ── TestCoerceYamlTypes (flattened) ─────────────────────────────────────────


def test_coerce_yaml_types_converts_datetime_sf_last_pulled() -> None:
    """sf_last_pulled datetime is coerced to ISO string."""
    import datetime as dt

    data = {"stage": "discover", "sf_last_pulled": dt.datetime(2026, 1, 1, 12, 0, 0)}
    fm = PursuitFrontmatter.model_validate(data)
    assert isinstance(fm.sf_last_pulled, str)
    assert fm.sf_last_pulled.startswith("2026-01-01")


def test_coerce_yaml_types_converts_date_sf_close_date() -> None:
    """sf_close_date date object is coerced to ISO string."""
    import datetime as dt

    data = {"stage": "discover", "sf_close_date": dt.date(2026, 6, 1)}
    fm = PursuitFrontmatter.model_validate(data)
    assert fm.sf_close_date == "2026-06-01"


def test_coerce_yaml_types_passthrough_non_dict() -> None:
    """Non-dict input is returned unchanged by the validator."""
    # model_validate raises ValidationError for non-dict — test the raw validator path
    from fieldkit.pursuit.models import PursuitFrontmatter

    result = PursuitFrontmatter.coerce_yaml_types("not-a-dict")
    assert result == "not-a-dict"


# ---------------------------------------------------------------------------
# 4A.2 normalize_monetary (PursuitFrontmatter field_validator)
# ---------------------------------------------------------------------------


# ── TestNormalizeMonetary (flattened) ───────────────────────────────────────


def test_normalize_monetary_parses_dollar_string() -> None:
    """'$500,000' is parsed to 500000.0."""
    fm = PursuitFrontmatter.model_validate({"stage": "discover", "sf_arr": "$500,000"})
    assert fm.sf_arr == 500000.0


def test_normalize_monetary_passthrough_float() -> None:
    """Plain float passthrough."""
    fm = PursuitFrontmatter.model_validate({"stage": "discover", "sf_arr": 500000.0})
    assert fm.sf_arr == 500000.0


def test_normalize_monetary_none_passthrough() -> None:
    """None returns None."""
    fm = PursuitFrontmatter.model_validate({"stage": "discover", "sf_arr": None})
    assert fm.sf_arr is None


def test_normalize_monetary_coerces_non_numeric_to_none() -> None:
    """'N/A' returns None (non-numeric strings coerced to None)."""
    with pytest.warns(UserWarning, match="normalize_monetary.*could not parse"):
        fm = PursuitFrontmatter.model_validate({"stage": "discover", "sf_arr": "N/A"})
    assert fm.sf_arr is None


# ---------------------------------------------------------------------------
# 4A.3 coerce_deal_splits (PursuitFrontmatter field_validator)
# ---------------------------------------------------------------------------


# ── TestCoerceDealSplits (flattened) ────────────────────────────────────────


def test_coerce_deal_splits_none_returns_none() -> None:
    fm = PursuitFrontmatter.model_validate({"stage": "discover", "sf_deal_splits": None})
    assert fm.sf_deal_splits is None


def test_coerce_deal_splits_empty_string_returns_none() -> None:
    fm = PursuitFrontmatter.model_validate({"stage": "discover", "sf_deal_splits": ""})
    assert fm.sf_deal_splits is None


def test_coerce_deal_splits_list_passthrough() -> None:
    data = [{"split_owner": "Alice", "split_pct": 50}]
    fm = PursuitFrontmatter.model_validate({"stage": "discover", "sf_deal_splits": data})
    assert fm.sf_deal_splits == data


def test_coerce_deal_splits_non_list_returns_none() -> None:
    fm = PursuitFrontmatter.model_validate({"stage": "discover", "sf_deal_splits": 42})
    assert fm.sf_deal_splits is None
