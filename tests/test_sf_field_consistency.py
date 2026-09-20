"""Invariant tests for SF field definitions.

Asserts that PursuitFrontmatter model fields, io.py round-trip, and
frontmatter.py strip-and-insert all stay synchronized through a single
source of truth: SF_FIELD_NAMES derived from PursuitFrontmatter.model_fields.

Adding a new sf_ field to PursuitFrontmatter automatically propagates to
all three surfaces — these tests catch any drift.
"""

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


# ── TestSFOppKeyMapSubsetOfModel (flattened) ────────────────────────────────


def test_sf_opp_key_map_subset_of_model_sf_opp_key_map_subset_of_model() -> None:
    from fieldkit.commands.sf.frontmatter import SF_OPP_KEY_MAP
    from fieldkit.pursuit.models import SF_FIELD_NAMES

    drift = set(SF_OPP_KEY_MAP.keys()) - SF_FIELD_NAMES
    assert not drift, (
        f"SF_OPP_KEY_MAP has keys not in PursuitFrontmatter: {sorted(drift)}. "
        "Add the field to PursuitFrontmatter or remove it from SF_OPP_KEY_MAP."
    )


# ── TestSFFieldNamesMatchesModelIntrospection (flattened) ───────────────────


def test_sf_field_names_matches_model_introspection_sf_field_names_matches_model_introspection() -> None:
    from fieldkit.pursuit.models import SF_FIELD_NAMES, PursuitFrontmatter

    derived = frozenset(k for k in PursuitFrontmatter.model_fields if k.startswith("sf_"))
    assert derived == SF_FIELD_NAMES, (
        f"SF_FIELD_NAMES is stale. "
        f"In model but not constant: {sorted(derived - SF_FIELD_NAMES)}. "
        f"In constant but not model: {sorted(SF_FIELD_NAMES - derived)}."
    )


def test_dead_opportunity_territory_is_absent_from_model_and_schema() -> None:
    from fieldkit.pursuit.models import SF_FIELD_NAMES, PursuitFrontmatter

    schema_path = Path(__file__).parent.parent / "src/fieldkit/_data/pursuit-frontmatter.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert "sf_opportunity_territory" not in PursuitFrontmatter.model_fields
    assert "sf_opportunity_territory" not in SF_FIELD_NAMES
    assert "sf_opportunity_territory" not in schema["properties"]


# ── TestIOSFFieldsEqualsSFFieldNames (flattened) ────────────────────────────


def test_iosf_fields_equals_sf_field_names_io_sf_fields_equals_sf_field_names() -> None:
    from fieldkit.pursuit.io import _SF_FIELDS
    from fieldkit.pursuit.models import SF_FIELD_NAMES

    assert _SF_FIELDS == SF_FIELD_NAMES, (
        f"fieldkit.io._SF_FIELDS diverged from SF_FIELD_NAMES. "
        f"Extra in _SF_FIELDS: {sorted(_SF_FIELDS - SF_FIELD_NAMES)}. "
        f"Missing from _SF_FIELDS: {sorted(SF_FIELD_NAMES - _SF_FIELDS)}."
    )


# ── TestStripSFKeyersCoversAllModelFields (flattened) ───────────────────────


def test_strip_sf_keyers_covers_all_model_fields_strip_sf_keys_covers_all_model_fields() -> None:
    from fieldkit.commands.sf.frontmatter import _strip_sf_keys
    from fieldkit.pursuit.models import SF_FIELD_NAMES

    # Build a synthetic frontmatter block: one line per sf_ field
    fm_lines = [f"{key}: some_value" for key in sorted(SF_FIELD_NAMES)]
    # Add a non-sf_ line that must survive
    fm_lines.append("stage: propose")

    result = _strip_sf_keys(fm_lines)

    # No sf_ lines should remain
    remaining_sf = [ln for ln in result if any(ln.startswith(f"{k}:") for k in SF_FIELD_NAMES)]
    assert not remaining_sf, (
        f"_strip_sf_keys() left sf_ lines in output: {remaining_sf}. "
        "Update _ALL_SF_KEYS in frontmatter.py to include all SF_FIELD_NAMES."
    )

    # The non-sf_ line must be preserved
    assert "stage: propose" in result, "_strip_sf_keys() removed a non-sf_ line — over-stripping bug."


# ── TestRoundTripPreservesSFFields (flattened) ──────────────────────────────


def test_round_trip_preserves_sf_fields_round_trip_preserves_sf_fields(tmp_path: pytest.TempdirFactory) -> None:
    from fieldkit.pursuit.io import load_pursuit, write_frontmatter
    from fieldkit.pursuit.models import SF_FIELD_NAMES

    # Build a pursuit file with all sf_ fields populated with recognizable values.
    # Monetary fields (float | None) must use numeric values to avoid
    # normalize_monetary warnings; all other sf_ fields use string placeholders.
    _monetary_fields = {"sf_arr", "sf_acv", "sf_consulting_acv", "sf_training_acv", "sf_probability"}
    sf_parts: list[str] = []
    for key in sorted(SF_FIELD_NAMES):
        if key in _monetary_fields:
            sf_parts.append(f"{key}: 12345.67")
        else:
            sf_parts.append(f'{key}: "value_for_{key}"')
    sf_lines = "\n".join(sf_parts)
    content = f"---\nstage: propose\n{sf_lines}\n---\n\n# Round-trip test pursuit\n"
    pursuit_file = tmp_path / "test_pursuit.md"
    pursuit_file.write_text(content, encoding="utf-8")

    # Load → write → reload
    model1, body1, _ = load_pursuit(pursuit_file)
    write_frontmatter(pursuit_file, model1, body1)
    model2, _body2, _ = load_pursuit(pursuit_file)

    # Every sf_ field must survive the round-trip without loss or corruption
    lost_fields = []
    corrupted_fields = []
    for field in sorted(SF_FIELD_NAMES):
        val1 = getattr(model1, field, None)
        val2 = getattr(model2, field, None)
        if val2 is None and val1 is not None:
            lost_fields.append(field)
        elif val1 != val2:
            corrupted_fields.append(f"{field}: {val1!r} → {val2!r}")

    assert not lost_fields, f"Fields lost during round-trip: {lost_fields}"
    assert not corrupted_fields, f"Fields corrupted during round-trip: {corrupted_fields}"
