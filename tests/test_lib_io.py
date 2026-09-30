"""Tests for lib.io — load_pursuit / write_frontmatter round-trip."""

import datetime
import shutil
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"
PURSUIT_SIMPLE = FIXTURES / "pursuit_simple.md"
PURSUIT_FULL = FIXTURES / "pursuit_full.md"


# ── TestImportSmoke (flattened) ─────────────────────────────────────────────


def test_import_smoke_import():
    from fieldkit.pursuit.io import load_pursuit, write_frontmatter

    assert callable(load_pursuit)
    assert callable(write_frontmatter)


# ── TestLoadPursuit (flattened) ─────────────────────────────────────────────


@pytest.mark.integration
def test_load_pursuit_simple_fixture_stage():
    from fieldkit.pursuit.io import load_pursuit

    model, _body, _ = load_pursuit(PURSUIT_SIMPLE)
    assert model.stage == "discover"


@pytest.mark.integration
def test_load_pursuit_full_fixture_does_not_invent_legacy_composite():
    from fieldkit.pursuit.io import load_pursuit

    model, _body, _ = load_pursuit(PURSUIT_FULL)
    assert model.legacy_meddpicc is not None
    assert "composite" not in model.legacy_meddpicc.model_dump(exclude_unset=True)


@pytest.mark.integration
def test_load_pursuit_full_fixture_returns_tuple():
    from fieldkit.pursuit.io import load_pursuit
    from fieldkit.pursuit.models import PursuitFrontmatter

    result = load_pursuit(PURSUIT_FULL)
    assert isinstance(result, tuple)
    assert len(result) == 3
    model, body, mtime = result
    assert isinstance(model, PursuitFrontmatter)
    assert isinstance(body, str)
    assert isinstance(mtime, float)


@pytest.mark.integration
def test_load_pursuit_populated_salesforce_file_stage(sf_pursuit: Path) -> None:
    from fieldkit.pursuit.io import load_pursuit

    result = load_pursuit(sf_pursuit)
    assert len(result) == 3
    model, body, _mtime = result
    assert model.stage == "propose"
    assert body.startswith("\n\n# Service Expansion")


# ── TestWriteFrontmatter (flattened) ────────────────────────────────────────


def test_write_frontmatter_round_trip_preserves_fields(tmp_path):
    from fieldkit.pursuit.io import load_pursuit, write_frontmatter

    # Copy full fixture to tmp
    src = PURSUIT_FULL
    dst = tmp_path / "pursuit_full.md"
    shutil.copy(src, dst)

    model1, body1, _ = load_pursuit(dst)
    write_frontmatter(dst, model1, body1)
    model2, body2, _ = load_pursuit(dst)

    assert model1.stage == model2.stage
    assert model1.legacy_meddpicc is not None
    assert model2.legacy_meddpicc is not None
    assert model1.legacy_meddpicc.composite == model2.legacy_meddpicc.composite
    assert model1.sf_opportunity_id == model2.sf_opportunity_id
    assert body1 == body2


def test_write_frontmatter_round_trip_simple_fixture(tmp_path):
    from fieldkit.pursuit.io import load_pursuit, write_frontmatter

    src = PURSUIT_SIMPLE
    dst = tmp_path / "pursuit_simple.md"
    shutil.copy(src, dst)

    model1, body1, _ = load_pursuit(dst)
    write_frontmatter(dst, model1, body1)
    model2, body2, _ = load_pursuit(dst)

    assert model1.stage == model2.stage
    assert body1 == body2


def test_legacy_opportunity_territory_survives_unrelated_round_trip(tmp_path: Path) -> None:
    from fieldkit.pursuit.enums import Stage
    from fieldkit.pursuit.io import load_pursuit, write_frontmatter

    path = tmp_path / "legacy.md"
    path.write_text(
        "---\nstage: discover\nsf_opportunity_territory: LEGACY_TERRITORY\n---\n\n# Legacy pursuit\n",
        encoding="utf-8",
    )
    model, body, mtime = load_pursuit(path)
    model.stage = Stage.VALIDATE

    write_frontmatter(path, model, body, expected_mtime=mtime)

    written = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])
    assert written["stage"] == "validate"
    assert written["sf_opportunity_territory"] == "LEGACY_TERRITORY"


# ── TestRoundTripIdempotent (flattened) ─────────────────────────────────────


def test_round_trip_idempotent_double_write_is_identical(tmp_path):
    from fieldkit.pursuit.io import load_pursuit, write_frontmatter

    src = PURSUIT_FULL
    dst = tmp_path / "idempotent.md"
    shutil.copy(src, dst)

    # First write
    model1, body1, _ = load_pursuit(dst)
    write_frontmatter(dst, model1, body1)
    after_first = dst.read_text(encoding="utf-8")

    # Second write
    model2, body2, _ = load_pursuit(dst)
    write_frontmatter(dst, model2, body2)
    after_second = dst.read_text(encoding="utf-8")

    assert after_first == after_second, "Second write differs from first — write_frontmatter is not idempotent"


def test_round_trip_idempotent_double_write_simple_is_identical(tmp_path):
    from fieldkit.pursuit.io import load_pursuit, write_frontmatter

    src = PURSUIT_SIMPLE
    dst = tmp_path / "idempotent_simple.md"
    shutil.copy(src, dst)

    model1, body1, _ = load_pursuit(dst)
    write_frontmatter(dst, model1, body1)
    after_first = dst.read_text(encoding="utf-8")

    model2, body2, _ = load_pursuit(dst)
    write_frontmatter(dst, model2, body2)
    after_second = dst.read_text(encoding="utf-8")

    assert after_first == after_second


# ── Populated Salesforce round-trip contracts ─────────────────────────────


@pytest.mark.integration
def test_populated_file_round_trip_sf_fields_preserved(sf_pursuit: Path) -> None:
    from fieldkit.pursuit.io import load_pursuit, write_frontmatter

    result = load_pursuit(sf_pursuit)
    assert len(result) == 3
    model1, body1, _mtime = result
    assert model1.sf_close_date == "2026-12-15"
    assert model1.sf_next_steps == "Review: scope #1 with sponsor"
    write_frontmatter(sf_pursuit, model1, body1)
    model2, body2, _ = load_pursuit(sf_pursuit)

    assert model1.sf_opportunity_id == model2.sf_opportunity_id
    assert model1.sf_stage == model2.sf_stage
    assert model1.sf_close_date == model2.sf_close_date
    assert model1.sf_arr == model2.sf_arr
    assert model1.sf_owner == model2.sf_owner
    assert model1.sf_next_steps == model2.sf_next_steps
    assert model1.sf_last_pulled == model2.sf_last_pulled
    assert body1 == body2


@pytest.mark.integration
def test_populated_file_round_trip_body_starts_with_expected_heading(sf_pursuit: Path) -> None:
    from fieldkit.pursuit.io import load_pursuit, write_frontmatter

    result = load_pursuit(sf_pursuit)
    assert len(result) == 3
    model, body, _mtime = result
    write_frontmatter(sf_pursuit, model, body)
    _model2, body2, _ = load_pursuit(sf_pursuit)

    assert body2.startswith("\n\n# Service Expansion")
    assert "Fictional scope: consulting services for Acme Corp." in body2


@pytest.mark.integration
def test_populated_file_canonical_round_trip_is_byte_identical(sf_pursuit: Path) -> None:
    """Canonical writing is idempotent after normalizing hand-authored YAML."""
    from fieldkit.pursuit.io import load_pursuit, write_frontmatter

    result = load_pursuit(sf_pursuit)
    assert len(result) == 3
    model, body, _mtime = result
    write_frontmatter(sf_pursuit, model, body)
    canonical = sf_pursuit.read_bytes()
    model2, body2, _ = load_pursuit(sf_pursuit)
    write_frontmatter(sf_pursuit, model2, body2)

    assert sf_pursuit.read_bytes() == canonical


# ── TestYAMLCoercion (flattened) ────────────────────────────────────────────


@pytest.mark.parametrize(
    "yaml_fragment, expected_type, expected_value",
    [
        # Unquoted ISO date → datetime.date
        (
            "sf_close_date: 2026-05-17",
            datetime.date,
            datetime.date(2026, 5, 17),
        ),
        # Unquoted datetime → datetime.datetime
        (
            "sf_close_date: 2026-05-17T14:30:00",
            datetime.datetime,
            datetime.datetime(2026, 5, 17, 14, 30),
        ),
        # Quoted date → str (no coercion)
        (
            'sf_close_date: "2026-05-17"',
            str,
            "2026-05-17",
        ),
        # Integer stays int
        (
            "sf_arr: 100000",
            int,
            100000,
        ),
        # null → None
        (
            "sf_close_date: null",
            type(None),
            None,
        ),
    ],
)
def test_yaml_scalar_safe_load_coercion(yaml_fragment, expected_type, expected_value):
    """yaml.safe_load coerces scalars according to the YAML 1.1 spec."""
    parsed = yaml.safe_load(yaml_fragment)
    # parsed is a dict with one key; grab the value regardless of key name
    key = next(iter(parsed))
    val = parsed[key]
    assert type(val) is expected_type, (
        f"Expected type {expected_type.__name__}, got {type(val).__name__} for: {yaml_fragment!r}"
    )
    assert val == expected_value, f"Expected value {expected_value!r}, got {val!r} for: {yaml_fragment!r}"


def test_yaml_scalar_date_round_trip(tmp_path):
    """An ISO date normalized to the model's string type stays a string on write.

    ``load_pursuit`` accepts a historical bare YAML date, but the typed model
    normalizes ``sf_close_date`` to ``str``. The canonical writer must quote
    that date-like string so a subsequent YAML load does not re-type it.
    """
    from fieldkit.pursuit.io import load_pursuit, write_frontmatter

    # Build a pursuit file that contains an unquoted sf_close_date
    original = PURSUIT_SIMPLE.read_text(encoding="utf-8")

    # Inject sf_close_date into the frontmatter block (before the closing ---)
    # The simple fixture ends its frontmatter with '  paper-process: 0\n---'
    modified = original.replace(
        "\n---\n",
        "\nsf_close_date: 2026-05-17\n---\n",
        1,  # replace only the first occurrence (closing delimiter)
    )
    tmp_file = tmp_path / "pursuit_date.md"
    tmp_file.write_text(modified, encoding="utf-8")

    # load_pursuit coerces datetime.date to ISO string (sf_close_date is str | None)
    model, body, _ = load_pursuit(tmp_file)
    assert model.sf_close_date == "2026-05-17", (
        f"load_pursuit should coerce datetime.date to ISO string; got {model.sf_close_date!r}"
    )

    # write_frontmatter preserves the model's string type by quoting the value.
    write_frontmatter(tmp_file, model, body)

    # Reload the raw frontmatter and confirm the canonical output remains a string.
    reloaded_text = tmp_file.read_text(encoding="utf-8")
    # Extract frontmatter block manually for targeted assertion
    lines = reloaded_text.split("\n")
    dashes = [i for i, ln in enumerate(lines) if ln.strip() == "---"]
    fm_text = "\n".join(lines[dashes[0] + 1 : dashes[1]])
    reloaded = yaml.safe_load(fm_text)

    assert reloaded["sf_close_date"] == "2026-05-17"


# ── TestStalenessGuard (flattened) ──────────────────────────────────────────


def test_staleness_guard_stale_model_raises_value_error(tmp_path):
    """write_frontmatter with a stale expected_mtime raises ValueError."""

    from fieldkit.pursuit.io import load_pursuit, write_frontmatter

    src = PURSUIT_SIMPLE
    dst = tmp_path / "pursuit_simple.md"
    shutil.copy(src, dst)

    model, body, mtime = load_pursuit(dst)

    # Simulate an intervening modification by touching the file with a future mtime
    future_mtime = mtime + 10.0
    import os

    os.utime(dst, (future_mtime, future_mtime))

    with pytest.raises(ValueError, match="stale model detected"):
        write_frontmatter(dst, model, body, expected_mtime=mtime)


def test_staleness_guard_fresh_model_does_not_raise(tmp_path):
    """write_frontmatter with the correct expected_mtime succeeds."""
    from fieldkit.pursuit.io import load_pursuit, write_frontmatter

    src = PURSUIT_SIMPLE
    dst = tmp_path / "pursuit_simple.md"
    shutil.copy(src, dst)

    model, body, mtime = load_pursuit(dst)

    # Should not raise — mtime matches
    write_frontmatter(dst, model, body, expected_mtime=mtime)
    model2, _, _ = load_pursuit(dst)
    assert model2.stage == model.stage


def test_staleness_guard_no_expected_mtime_skips_guard(tmp_path):
    """write_frontmatter without expected_mtime never raises for staleness."""
    import os

    from fieldkit.pursuit.io import load_pursuit, write_frontmatter

    src = PURSUIT_SIMPLE
    dst = tmp_path / "pursuit_simple.md"
    shutil.copy(src, dst)

    model, body, mtime = load_pursuit(dst)

    # Move the mtime forward — without expected_mtime, no guard fires
    os.utime(dst, (mtime + 10.0, mtime + 10.0))

    # Should not raise
    write_frontmatter(dst, model, body)
    assert dst.exists()


def test_staleness_guard_error_message_includes_path(tmp_path):
    """ValueError message includes the file path for diagnostics."""
    import os

    from fieldkit.pursuit.io import load_pursuit, write_frontmatter

    src = PURSUIT_SIMPLE
    dst = tmp_path / "pursuit_simple.md"
    shutil.copy(src, dst)

    model, body, mtime = load_pursuit(dst)
    os.utime(dst, (mtime + 1.0, mtime + 1.0))

    with pytest.raises(ValueError, match=str(dst)):
        write_frontmatter(dst, model, body, expected_mtime=mtime)


# ── TestDuplicateKeyDetection (flattened) ───────────────────────────────────

_RENDER_KEY_VALUE__DUPLICATE_FM = """\
---
title: Test
stage: discover
title: Duplicate Title
---
body text
"""


def test_render_key_value_load_pursuit_warns_on_duplicate_keys(tmp_path, caplog):
    """load_pursuit logs WARNING when YAML frontmatter has duplicate keys."""
    import logging

    from fieldkit.pursuit.io import load_pursuit

    f = tmp_path / "dup.md"
    f.write_text(_RENDER_KEY_VALUE__DUPLICATE_FM, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="fieldkit.pursuit.io"):
        _model, _body, _ = load_pursuit(f)

    assert any("title" in r.message.lower() or "duplicate" in r.message.lower() for r in caplog.records)


def test_render_key_value_write_frontmatter_raises_on_duplicate_keys(tmp_path):
    """write_frontmatter raises ValueError when source file has duplicate keys."""
    import shutil

    from fieldkit.pursuit.io import load_pursuit, write_frontmatter

    # Write a clean file first for load
    clean = PURSUIT_SIMPLE
    dst = tmp_path / "clean.md"
    shutil.copy(clean, dst)
    model, body, _mtime = load_pursuit(dst)

    # Replace file content with duplicates (bypass load validation)
    dst.write_text(_RENDER_KEY_VALUE__DUPLICATE_FM, encoding="utf-8")

    with pytest.raises(ValueError, match=r"[Dd]uplicate"):
        write_frontmatter(dst, model, body)

    # File should still contain the duplicate content (unchanged)
    assert "title: Test" in dst.read_text(encoding="utf-8")
