"""Tests for fieldkit pursuit archive command."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from fieldkit.commands.pursuit.archive_cmd import cli


def _make_pursuit(pursuits_dir: Path, slug: str, stage: str = "closed-won") -> Path:
    pursuits_dir.mkdir(parents=True, exist_ok=True)
    content = f"---\nstage: {stage}\ngate-status: pending\n---\n\n# {slug}\n"
    path = pursuits_dir / f"{slug}.md"
    path.write_text(content, encoding="utf-8")
    return path


# ── TestArchiveSingle (flattened) ───────────────────────────────────────────


@pytest.mark.unit
def test_archive_single_archives_named_pursuit(tmp_path: Path) -> None:
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    _make_pursuit(pursuits, "deal-one", stage="closed-won")

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        from unittest.mock import patch

        with patch("fieldkit.commands.pursuit.archive_cmd._data_root", return_value=tmp_path):
            result = runner.invoke(cli, ["--account", "acme", "--name", "deal-one"])

    assert result.exit_code == 0
    assert not (pursuits / "deal-one.md").exists()
    assert (tmp_path / "accounts" / "acme" / "archive" / "deal-one.md").exists()


@pytest.mark.unit
def test_archive_single_dry_run_does_not_move(tmp_path: Path) -> None:
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    _make_pursuit(pursuits, "deal-two")

    runner = CliRunner()
    from unittest.mock import patch

    with patch("fieldkit.commands.pursuit.archive_cmd._data_root", return_value=tmp_path):
        result = runner.invoke(cli, ["--account", "acme", "--name", "deal-two", "--dry-run"])

    assert result.exit_code == 0
    assert (pursuits / "deal-two.md").exists()  # not moved
    assert "dry-run" in result.output


@pytest.mark.unit
def test_archive_single_missing_pursuit_exits_3(tmp_path: Path) -> None:
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    pursuits.mkdir(parents=True)

    runner = CliRunner()
    from unittest.mock import patch

    with patch("fieldkit.commands.pursuit.archive_cmd._data_root", return_value=tmp_path):
        result = runner.invoke(cli, ["--account", "acme", "--name", "nonexistent"])

    assert result.exit_code == 3


# ── TestArchiveAllClosed (flattened) ────────────────────────────────────────


@pytest.mark.unit
def test_archive_all_closed_archives_only_closed_stages(tmp_path: Path) -> None:
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    _make_pursuit(pursuits, "won-deal", stage="closed-won")
    _make_pursuit(pursuits, "lost-deal", stage="closed-lost")
    _make_pursuit(pursuits, "active-deal", stage="discover")

    runner = CliRunner()
    from unittest.mock import patch

    with patch("fieldkit.commands.pursuit.archive_cmd._data_root", return_value=tmp_path):
        result = runner.invoke(cli, ["--account", "acme", "--all-closed"])

    assert result.exit_code == 0
    archive = tmp_path / "accounts" / "acme" / "archive"
    assert (archive / "won-deal.md").exists()
    assert (archive / "lost-deal.md").exists()
    assert not (archive / "active-deal.md").exists()
    assert (pursuits / "active-deal.md").exists()
