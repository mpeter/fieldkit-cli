"""Regression coverage for historical local MEDDPICC containment."""

import json
import os
from pathlib import Path

import jsonschema
import pytest
import yaml
from pydantic import ValidationError

from fieldkit.errors import FrontmatterStalenessError
from fieldkit.pursuit.io import load_pursuit, write_frontmatter, write_frontmatter_raw
from fieldkit.pursuit.models import LegacyMEDDPICC, PursuitFrontmatter

pytestmark = pytest.mark.unit

_SCHEMA_PATH = Path(__file__).parents[1] / "src" / "fieldkit" / "_data" / "pursuit-frontmatter.schema.json"


def _legacy_file(tmp_path: Path) -> Path:
    path = tmp_path / "legacy.md"
    path.write_text(
        "---\n"
        "title: Historical pursuit\n"
        "meddpicc:\n"
        "  metrics: unknown\n"
        "  champion: 7\n"
        "  paper-process:\n"
        "  composite: kept verbatim\n"
        "custom-key: keep me\n"
        "stage: discover\n"
        "---\n\n# Body\n\nKeep this exactly.\n",
        encoding="utf-8",
    )
    return path


def test_former_meddpicc_loads_as_typed_historical_data_without_mutating_file(tmp_path: Path) -> None:
    """Load former scores as typed history without changing the source file."""
    path = _legacy_file(tmp_path)
    original = path.read_bytes()
    original_mtime = path.stat().st_mtime_ns

    model, body, _ = load_pursuit(path)

    assert isinstance(model.legacy_meddpicc, LegacyMEDDPICC)
    assert model.legacy_meddpicc.schema_version == 1
    assert model.legacy_meddpicc.status == "historical"
    assert model.legacy_meddpicc.metrics == "unknown"
    assert model.legacy_meddpicc.champion == 7
    assert model.legacy_meddpicc.paper_process is None
    assert model.legacy_meddpicc.composite == "kept verbatim"
    assert body == "\n\n# Body\n\nKeep this exactly.\n"
    assert "meddpicc" not in PursuitFrontmatter.model_fields
    assert not hasattr(model, "meddpicc")
    assert not hasattr(model.legacy_meddpicc, "total")
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == original_mtime


def test_partial_and_malformed_values_are_preserved_without_defaults_or_clamping() -> None:
    """Preserve irregular historical values without interpreting or repairing them."""
    model = PursuitFrontmatter.model_validate(
        {
            "stage": "discover",
            "meddpicc": {
                "champion": -4,
                "economic-buyer": "not scored",
                "future-element": {"raw": True},
            },
        }
    )

    assert model.legacy_meddpicc is not None
    dumped = model.legacy_meddpicc.model_dump(by_alias=True, exclude_unset=True)
    assert dumped == {
        "schema_version": 1,
        "status": "historical",
        "champion": -4,
        "economic-buyer": "not scored",
        "future-element": {"raw": True},
    }


def test_canonical_legacy_shape_requires_fixed_version_and_status() -> None:
    """Reject historical envelopes with unsupported metadata."""
    with pytest.raises(ValidationError, match="schema_version"):
        PursuitFrontmatter.model_validate(
            {"stage": "discover", "legacy_meddpicc": {"schema_version": 2, "status": "historical"}}
        )

    with pytest.raises(ValidationError, match="status"):
        PursuitFrontmatter.model_validate(
            {"stage": "discover", "legacy_meddpicc": {"schema_version": 1, "status": "current"}}
        )


@pytest.mark.parametrize("version", [True, "1"], ids=("boolean", "numeric-string"))
def test_canonical_legacy_shape_rejects_nonnumeric_version_lookalikes(version: object) -> None:
    """Keep canonical model validation aligned with the committed JSON Schema."""
    with pytest.raises(ValidationError, match="schema_version must be the number 1"):
        PursuitFrontmatter.model_validate(
            {"stage": "discover", "legacy_meddpicc": {"schema_version": version, "status": "historical"}}
        )


def test_both_legacy_keys_fail_closed() -> None:
    """Reject ambiguous files containing both former and canonical history keys."""
    with pytest.raises(ValidationError, match="both meddpicc and legacy_meddpicc"):
        PursuitFrontmatter.model_validate(
            {
                "stage": "discover",
                "meddpicc": {"metrics": 2},
                "legacy_meddpicc": {"schema_version": 1, "status": "historical", "metrics": 2},
            }
        )


def test_authorized_model_write_canonicalizes_in_place_and_preserves_content(tmp_path: Path) -> None:
    """Canonicalize history only during an authorized atomic model write."""
    path = _legacy_file(tmp_path)
    model, body, mtime = load_pursuit(path)

    write_frontmatter(path, model, body, expected_mtime=mtime)

    written = path.read_text(encoding="utf-8")
    raw = yaml.safe_load(written.split("---", 2)[1])
    assert list(raw) == ["title", "legacy_meddpicc", "custom-key", "stage"]
    assert raw["legacy_meddpicc"] == {
        "schema_version": 1,
        "status": "historical",
        "metrics": "unknown",
        "champion": 7,
        "paper-process": None,
        "composite": "kept verbatim",
    }
    assert "meddpicc" not in raw
    assert written.endswith("---\n\n# Body\n\nKeep this exactly.\n")


def test_authorized_raw_write_canonicalizes_old_disk_key_and_honors_updates(tmp_path: Path) -> None:
    """Canonicalize former history while retaining an authorized raw update."""
    path = _legacy_file(tmp_path)
    _, body, mtime = load_pursuit(path)
    fm = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])
    fm["stage"] = "validate"

    write_frontmatter_raw(path, fm, body, expected_mtime=mtime)

    raw = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])
    assert list(raw) == ["title", "legacy_meddpicc", "custom-key", "stage"]
    assert raw["stage"] == "validate"
    assert raw["legacy_meddpicc"]["metrics"] == "unknown"
    assert raw["legacy_meddpicc"]["composite"] == "kept verbatim"
    assert "meddpicc" not in raw


@pytest.mark.parametrize("value", ["Note: 😀", "2026-02-30"])
def test_authorized_raw_write_preserves_quoted_unrelated_scalars(tmp_path: Path, value: str) -> None:
    """Keep unrelated quoted scalar semantics during history canonicalization."""
    path = tmp_path / "legacy-scalar.md"
    path.write_text(
        f"---\ntitle: {json.dumps(value, ensure_ascii=False)}\nstage: discover\nmeddpicc:\n  metrics: 2\n---\n\n# Body\n",
        encoding="utf-8",
    )
    _, body, mtime = load_pursuit(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])

    write_frontmatter_raw(path, raw, body, expected_mtime=mtime)

    written = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])
    assert written["title"] == value
    assert written["legacy_meddpicc"]["metrics"] == 2


@pytest.mark.parametrize("separator", ["\x85", "\u2028", "\u2029"])
@pytest.mark.parametrize("writer", ["model", "raw"])
def test_authorized_legacy_writes_preserve_nested_unicode_separators(
    tmp_path: Path,
    separator: str,
    writer: str,
) -> None:
    """Preserve YAML-sensitive Unicode inside nested historical values."""
    value = f"before{separator}after"
    escaped = value.encode("unicode_escape").decode("ascii")
    path = tmp_path / "legacy.md"
    path.write_text(
        f'---\nstage: discover\nmeddpicc:\n  future-element: "{escaped}"\n---\n\n# Body\n',
        encoding="utf-8",
    )
    model, body, mtime = load_pursuit(path)

    if writer == "model":
        write_frontmatter(path, model, body, expected_mtime=mtime)
    else:
        raw = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])
        write_frontmatter_raw(path, raw, body, expected_mtime=mtime)

    written = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])
    assert written["legacy_meddpicc"]["future-element"] == value


def test_stale_raw_write_does_not_canonicalize_or_modify_file(tmp_path: Path) -> None:
    """Leave stale files byte-identical instead of opportunistically migrating them."""
    path = _legacy_file(tmp_path)
    original = path.read_text(encoding="utf-8")
    _, body, mtime = load_pursuit(path)
    os.utime(path, (mtime + 10, mtime + 10))

    with pytest.raises(FrontmatterStalenessError, match="modified since last read"):
        write_frontmatter_raw(path, {"stage": "validate"}, body, expected_mtime=mtime)

    assert path.read_text(encoding="utf-8") == original


def test_raw_write_rejects_both_keys_without_modifying_file(tmp_path: Path) -> None:
    """Fail an ambiguous raw write before modifying its file."""
    path = _legacy_file(tmp_path)
    original = path.read_text(encoding="utf-8")
    _, body, mtime = load_pursuit(path)

    with pytest.raises(ValueError, match="both meddpicc and legacy_meddpicc"):
        write_frontmatter_raw(
            path,
            {
                "meddpicc": {"metrics": 2},
                "legacy_meddpicc": {"schema_version": 1, "status": "historical", "metrics": 2},
            },
            body,
            expected_mtime=mtime,
        )

    assert path.read_text(encoding="utf-8") == original


def test_create_path_rejects_active_local_scores(tmp_path: Path) -> None:
    """Reject active local scores on the new-pursuit creation path."""
    path = tmp_path / "new.md"

    with pytest.raises(ValueError, match="native ClosePlan workflow"):
        write_frontmatter_raw(path, {"stage": "discover", "meddpicc": {"metrics": 1}}, "\n", create=True)

    assert not path.exists()


@pytest.mark.parametrize(
    "payload",
    [
        {"stage": "discover", "gate-status": "pending"},
        {"stage": "discover", "gate-status": "pending", "meddpicc": None},
        {"stage": "discover", "gate-status": "pending", "legacy_meddpicc": None},
        {"stage": "discover", "gate-status": "pending", "meddpicc": {"champion": "unknown"}},
        {
            "stage": "discover",
            "gate-status": "pending",
            "legacy_meddpicc": {"schema_version": 1, "status": "historical", "champion": 9},
        },
    ],
)
def test_schema_accepts_unscored_and_read_compatible_historical_shapes(payload: dict[str, object]) -> None:
    """Accept unscored pursuits and supported read-compatible history shapes."""
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))

    jsonschema.validate(payload, schema)


@pytest.mark.parametrize(
    ("former", "canonical"),
    [
        ({}, {"schema_version": 1, "status": "historical"}),
        (None, None),
    ],
)
def test_schema_rejects_both_historical_representations(former: object, canonical: object) -> None:
    """Reject schema input containing both historical representations."""
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    payload = {
        "stage": "discover",
        "gate-status": "pending",
        "meddpicc": former,
        "legacy_meddpicc": canonical,
    }

    with pytest.raises(jsonschema.ValidationError, match="should not be valid"):
        jsonschema.validate(payload, schema)


@pytest.mark.parametrize(
    "qualification",
    [
        {"meddpicc": "invalid"},
        {"legacy_meddpicc": ["invalid"]},
        {"legacy_meddpicc": {}},
        {"legacy_meddpicc": {"schema_version": 2, "status": "historical"}},
        {"legacy_meddpicc": {"schema_version": 1, "status": "current"}},
    ],
)
def test_schema_rejects_malformed_non_null_historical_shapes(qualification: dict[str, object]) -> None:
    """Reject malformed non-null historical qualification mappings."""
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    payload = {"stage": "discover", "gate-status": "pending", **qualification}

    with pytest.raises(jsonschema.ValidationError) as exc_info:
        jsonschema.validate(payload, schema)

    assert exc_info.value.validator in {"const", "required", "type"}


@pytest.mark.parametrize(
    "former",
    [
        {"schema_version": 1, "metrics": 2},
        {"status": "historical", "metrics": 2},
        {"schema_version": 1, "status": "historical", "metrics": 2},
    ],
)
def test_schema_and_model_reject_reserved_metadata_in_former_meddpicc(former: dict[str, object]) -> None:
    """Prevent former score maps from impersonating canonical history envelopes."""
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    payload = {"stage": "discover", "gate-status": "pending", "meddpicc": former}

    with pytest.raises(jsonschema.ValidationError) as exc_info:
        jsonschema.validate(payload, schema)

    assert exc_info.value.validator == "not"
    assert list(exc_info.value.absolute_path) == ["meddpicc"]
    assert exc_info.value.validator_value == schema["properties"]["meddpicc"]["then"]["not"]
    with pytest.raises(ValidationError, match="reserved schema_version or status"):
        PursuitFrontmatter.model_validate(payload)


@pytest.mark.parametrize("qualification", [{"meddpicc": None}, {"legacy_meddpicc": None}])
def test_authorized_raw_write_omits_null_history_without_fabrication(
    tmp_path: Path, qualification: dict[str, object]
) -> None:
    """Omit null history during an authorized write without fabricating evidence."""
    path = tmp_path / "null-history.md"
    path.write_text("---\nstage: discover\ngate-status: pending\n---\n\n# Deal\n", encoding="utf-8")
    _, body, mtime = load_pursuit(path)

    write_frontmatter_raw(
        path,
        {"stage": "validate", "gate-status": "pending", **qualification},
        body,
        expected_mtime=mtime,
    )

    raw = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])
    assert "meddpicc" not in raw
    assert "legacy_meddpicc" not in raw
    assert raw["stage"] == "validate"


def test_create_path_rejects_former_active_score_key(tmp_path: Path) -> None:
    """Reject the former active-score key when creating a pursuit."""
    path = tmp_path / "new.md"

    with pytest.raises(ValueError, match="cannot create active local MEDDPICC scores"):
        write_frontmatter_raw(path, {"stage": "discover", "meddpicc": {"metrics": 2}}, "\nBody\n", create=True)

    assert not path.exists()
