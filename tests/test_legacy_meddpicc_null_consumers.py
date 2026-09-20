"""Regression coverage for null historical MEDDPICC frontmatter."""

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from pydantic import ValidationError

from fieldkit.commands.pipeline.collect import collect_pursuit_health
from fieldkit.pursuit.enums import Stage
from fieldkit.pursuit.io import load_pursuit, write_frontmatter
from fieldkit.pursuit.models import PursuitFrontmatter
from fieldkit.watch import close_date_countdown as countdown

pytestmark = pytest.mark.unit


def _write_pursuit(tmp_path: Path, qualification_line: str) -> Path:
    path = tmp_path / "accounts" / "acme" / "pursuits" / "deal.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        "stage: propose\n"
        "sf_opportunity_id: 006example\n"
        "sf_close_date: 2026-09-20\n"
        f"{qualification_line}\n"
        "---\n\n"
        "# Deal\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("qualification_line", ["meddpicc: null", "meddpicc:", "legacy_meddpicc: null"])
def test_null_historical_qualification_loads_as_absent(tmp_path: Path, qualification_line: str) -> None:
    path = _write_pursuit(tmp_path, qualification_line)

    model, body, _mtime = load_pursuit(path)

    assert model.legacy_meddpicc is None
    assert body == "\n\n# Deal\n"


@pytest.mark.parametrize("qualification_line", ["meddpicc: null", "legacy_meddpicc: null"])
def test_typed_write_omits_null_history_and_preserves_other_content(tmp_path: Path, qualification_line: str) -> None:
    path = _write_pursuit(tmp_path, qualification_line)
    model, body, mtime = load_pursuit(path)
    model.stage = Stage.VALIDATE

    write_frontmatter(path, model, body, expected_mtime=mtime)

    written = path.read_text(encoding="utf-8")
    raw = yaml.safe_load(written.split("---", 2)[1])
    assert raw["stage"] == "validate"
    assert raw["sf_opportunity_id"] == "006example"
    assert raw["sf_close_date"] == "2026-09-20"
    assert "meddpicc" not in raw
    assert "legacy_meddpicc" not in raw
    assert written.endswith("---\n\n# Deal\n")


@pytest.mark.parametrize(
    "payload",
    [
        {"stage": "propose", "meddpicc": "invalid"},
        {"stage": "propose", "legacy_meddpicc": ["invalid"]},
    ],
)
def test_non_null_historical_qualification_shapes_still_fail_closed(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="must be a mapping"):
        PursuitFrontmatter.model_validate(payload)


def test_null_old_and_canonical_keys_still_fail_as_collision() -> None:
    with pytest.raises(ValidationError, match="both meddpicc and legacy_meddpicc"):
        PursuitFrontmatter.model_validate({"stage": "propose", "meddpicc": None, "legacy_meddpicc": None})


@pytest.mark.parametrize("qualification_line", ["meddpicc: null", "legacy_meddpicc: null"])
def test_pipeline_keeps_pursuit_with_null_historical_qualification(tmp_path: Path, qualification_line: str) -> None:
    _write_pursuit(tmp_path, qualification_line)

    rows = collect_pursuit_health(tmp_path)

    assert len(rows) == 1
    assert rows[0].deal == "deal"
    assert rows[0].native_qualification == "pending (live ClosePlan fetch required)"


@pytest.mark.parametrize("qualification_line", ["meddpicc:", "legacy_meddpicc: null"])
def test_countdown_keeps_pursuit_with_null_historical_qualification(tmp_path: Path, qualification_line: str) -> None:
    path = _write_pursuit(tmp_path, qualification_line)
    state: dict[str, object] = {}

    with (
        patch.object(countdown, "extract_champion_name", return_value=""),
        patch.object(countdown, "get_fieldkit_home", return_value=tmp_path),
        patch.object(countdown, "append_countdown_alert") as append_alert,
    ):
        result = countdown._process_pursuit_path(
            path,
            account_filter=None,
            today=date(2026, 9, 12),
            threshold_red=14,
            threshold_yellow=30,
            threshold_green=60,
            updated_state=state,
            dry_run=True,
        )

    assert result == (1, 1, 0, 0)
    alert = append_alert.call_args.args[0]
    assert alert["pursuit"] == "deal"
    assert alert["native_qualification"] == "pending (live ClosePlan fetch required)"
