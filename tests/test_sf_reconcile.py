"""Pure table reconciliation and guarded CLI publication contracts."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import fieldkit.sf.frontmatter as frontmatter
from fieldkit.commands.sf import reconcile
from fieldkit.errors import FrontmatterStalenessError
from fieldkit.pursuit.io import read_pursuit_text_snapshot
from fieldkit.sf.reconciliation import reconcile_key_fields
from tests.test_sf_sync_domain import OPPORTUNITY_ID, pursuit

pytestmark = pytest.mark.unit
TABLE = "\n## Key Fields\n\n| Field | Value |\n| ----- | ----- |\n| Stage | Discover |\n| ACV | 100 |\n| Owner | Example |\n"


def test_pure_transform_preserves_unmapped_rows_and_emits_no_output(capsys: pytest.CaptureFixture[str]) -> None:
    result = reconcile_key_fields(TABLE, {"sf_stage": "Propose", "sf_arr": 200})
    assert len(result.changes) == 2
    assert "| Stage | Propose |" in result.body
    assert "| ACV | 200 |" in result.body
    assert "| Owner | Example |" in result.body
    assert capsys.readouterr().out == capsys.readouterr().err == ""


@pytest.mark.parametrize("values", [{}, {"sf_stage": ""}, {"sf_stage": None}, {"sf_stage": "Discover"}])
def test_unchanged_table_is_byte_preserved(values: dict[str, object]) -> None:
    result = reconcile_key_fields(TABLE, values)
    assert result.body == TABLE
    assert result.changes == ()


def test_transform_does_not_cross_another_section() -> None:
    body = TABLE.replace("## Key Fields", "## Other")
    result = reconcile_key_fields("## Key Fields\nno table\n" + body, {"sf_stage": "Propose"})
    assert result.body == "## Key Fields\nno table\n" + body
    assert result.changes == ()


def test_table_values_cannot_inject_markdown_rows() -> None:
    result = reconcile_key_fields(TABLE, {"sf_stage": "Propose\n| injected | value |"})
    assert len(result.changes) == 1
    assert "\n| injected" not in result.body
    assert "&#124;" in result.body


@pytest.mark.parametrize("dry_run", [False, True])
def test_cli_reconciles_with_the_same_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    path = pursuit(tmp_path, identity=OPPORTUNITY_ID)
    path.write_text(
        path.read_text(encoding="utf-8").replace("gate-status: pending", "gate-status: pending\nsf_stage: Propose"),
        encoding="utf-8",
    )
    original = path.read_bytes()
    monkeypatch.setattr(reconcile, "get_fieldkit_home", lambda: tmp_path)
    result = CliRunner().invoke(reconcile.cli, [str(path), "--json", *(["--dry-run"] if dry_run else [])])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "updated"
    if dry_run:
        assert path.read_bytes() == original
    else:
        assert "| Stage | Propose |" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "content", [None, "no frontmatter", "---\n{{bad\n---\n", "---\nstage: discover\nstage: propose\n---\n"]
)
def test_dry_and_live_missing_or_invalid_targets_have_identical_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | None
) -> None:
    path = pursuit(tmp_path)
    if content is None:
        path.unlink()
    else:
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(reconcile, "get_fieldkit_home", lambda: tmp_path)
    live = CliRunner().invoke(reconcile.cli, [str(path), "--json"])
    dry = CliRunner().invoke(reconcile.cli, [str(path), "--json", "--dry-run"])
    assert live.exit_code == dry.exit_code == 3
    assert json.loads(live.stdout)["status"] == json.loads(dry.stdout)["status"] == "error"
    if content is not None:
        assert path.read_text(encoding="utf-8") == content


def test_reconciliation_refuses_same_mtime_source_change_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    path = pursuit(tmp_path, identity=OPPORTUNITY_ID)
    path.write_text(
        path.read_text(encoding="utf-8").replace("gate-status: pending", "gate-status: pending\nsf_stage: Propose"),
        encoding="utf-8",
    )
    publication = frontmatter._publish_validated_update
    changed = b""

    def intervene(plan: frontmatter._PublicationPlan) -> None:
        nonlocal changed
        info = path.stat()
        path.write_text(path.read_text(encoding="utf-8") + "\nConcurrent edit\n", encoding="utf-8")
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        changed = path.read_bytes()
        publication(plan)

    monkeypatch.setattr(frontmatter, "_publish_validated_update", intervene)
    with pytest.raises(FrontmatterStalenessError, match="changed"):
        frontmatter.reconcile_salesforce_pursuit(path, workspace=tmp_path, dry_run=False)
    assert path.read_bytes() == changed


def test_reconcile_publisher_uses_a_named_bounded_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.errors import SalesforceSyncPartialError

    path = pursuit(tmp_path, identity=OPPORTUNITY_ID)
    path.write_text(
        path.read_text(encoding="utf-8").replace("gate-status: pending", "gate-status: pending\nsf_stage: Propose"),
        encoding="utf-8",
    )
    observed: list[object] = []

    def locked(*args: object, **kwargs: object) -> None:
        observed.append(kwargs["timeout_seconds"])
        raise TimeoutError("unavailable")

    monkeypatch.setattr(frontmatter, "write_frontmatter_raw", locked)
    with pytest.raises(SalesforceSyncPartialError, match="publication could not be verified"):
        frontmatter.reconcile_salesforce_pursuit(path, workspace=tmp_path, dry_run=False)
    assert observed == [5]


@pytest.mark.parametrize("dry_run", [False, True])
def test_reconcile_refuses_symlinked_target_without_reading_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    from fieldkit.errors import FieldkitError

    path = pursuit(tmp_path, identity=OPPORTUNITY_ID)
    outside = tmp_path / "outside.md"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    reads: list[Path] = []

    def spy(target: Path) -> object:
        reads.append(target)
        return read_pursuit_text_snapshot(target)

    monkeypatch.setattr(frontmatter, "read_pursuit_text_snapshot", spy)
    with pytest.raises(FieldkitError, match="approved workspace"):
        frontmatter.reconcile_salesforce_pursuit(path, workspace=tmp_path, dry_run=dry_run)
    assert reads == []
