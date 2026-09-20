"""Coverage-gap tests for fieldkit pursuit archive `cli`.

Fills the branches left uncovered by tests/test_pursuit_archive.py: the
usage-error guard, the missing-pursuits-dir guard, the single-pursuit JSON
emission path, the all-closed template-file skip, the load_pursuit exception
path, and the errors-increment / exit-code gate in --all-closed mode.

Written independently of tests/test_pursuit_archive.py — helpers below are
local to this file, not imported from it, even though the fixture style is
intentionally similar (real tmp_path .md files with frontmatter, patching
``_data_root``).
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.pursuit.archive_cmd import cli

pytestmark = pytest.mark.unit


def _make_pursuit_file(pursuits_dir: Path, slug: str, stage: str = "closed-won") -> Path:
    pursuits_dir.mkdir(parents=True, exist_ok=True)
    content = f"---\nstage: {stage}\ngate-status: pending\n---\n\n# {slug}\n"
    path = pursuits_dir / f"{slug}.md"
    path.write_text(content, encoding="utf-8")
    return path


def _make_malformed_pursuit_file(pursuits_dir: Path, slug: str) -> Path:
    """Write a pursuit file with no YAML frontmatter delimiters at all.

    load_pursuit's _split_frontmatter raises ValueError when it finds fewer
    than two '---' lines, which is the exception the cli's all-closed loop
    is expected to swallow.
    """
    pursuits_dir.mkdir(parents=True, exist_ok=True)
    content = f"this file has no frontmatter delimiters\n# {slug}\n"
    path = pursuits_dir / f"{slug}.md"
    path.write_text(content, encoding="utf-8")
    return path


# ── 1. usage error when neither --name nor --all-closed is given ───────────


@pytest.mark.unit
def test_cli_neither_name_nor_all_closed_errors_without_touching_pursuits_dir(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    with patch(
        "fieldkit.commands.pursuit.archive_cmd._data_root",
        side_effect=AssertionError("_data_root must not be called"),
    ):
        result = runner.invoke(cli, ["--account", "acme"])

    assert result.exit_code == 1
    assert "ERROR: pass --name <slug> or --all-closed." in result.output


# ── 2. missing pursuits_dir guard ───────────────────────────────────────────


@pytest.mark.unit
def test_cli_missing_pursuits_dir_exits_3(tmp_path: Path) -> None:
    # No accounts/acme/pursuits directory is created under tmp_path.
    runner = CliRunner()
    with patch("fieldkit.commands.pursuit.archive_cmd._data_root", return_value=tmp_path):
        result = runner.invoke(cli, ["--account", "acme", "--name", "deal-one"])

    assert result.exit_code == 3
    expected_dir = tmp_path / "accounts" / "acme" / "pursuits"
    assert f"ERROR: pursuits directory not found: {expected_dir}" in result.output


# ── 3/4. single-pursuit --json emission path ────────────────────────────────


@pytest.mark.unit
def test_cli_single_json_success(tmp_path: Path) -> None:
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    _make_pursuit_file(pursuits, "deal-one", stage="closed-won")

    runner = CliRunner()
    with patch("fieldkit.commands.pursuit.archive_cmd._data_root", return_value=tmp_path):
        result = runner.invoke(cli, ["--account", "acme", "--name", "deal-one", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["archived"] == 1
    assert payload["skipped"] == 0
    assert payload["errors"] == 0
    assert payload["count"] == 1
    assert len(payload["items"]) == 1
    assert payload["items"][0]["pursuit"] == "deal-one.md"
    assert payload["items"][0]["ok"] is True


@pytest.mark.unit
def test_cli_single_json_failure(tmp_path: Path) -> None:
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    target = _make_pursuit_file(pursuits, "deal-two", stage="closed-won")

    fail_record = {
        "pursuit": "deal-two.md",
        "source": str(target),
        "dest": None,
        "outcome": "error",
        "ok": False,
    }

    runner = CliRunner()
    with (
        patch("fieldkit.commands.pursuit.archive_cmd._data_root", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.archive_cmd._archive_one", return_value=fail_record),
    ):
        result = runner.invoke(cli, ["--account", "acme", "--name", "deal-two", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["archived"] == 0
    assert payload["errors"] == 1
    assert len(payload["items"]) == 1
    assert payload["items"][0]["ok"] is False


# ── 5. --all-closed skips template.md silently (invisible to counters) ─────


@pytest.mark.unit
def test_cli_all_closed_skips_template_file_silently(tmp_path: Path) -> None:
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    # Give template.md closed-won stage frontmatter so, if the skip were
    # ever removed, it would actually get archived and change the counts.
    _make_pursuit_file(pursuits, "template", stage="closed-won")
    _make_pursuit_file(pursuits, "won-deal", stage="closed-won")

    runner = CliRunner()
    with patch("fieldkit.commands.pursuit.archive_cmd._data_root", return_value=tmp_path):
        result = runner.invoke(cli, ["--account", "acme", "--all-closed", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["archived"] == 1
    assert payload["skipped"] == 0
    assert payload["errors"] == 0
    names = {item["pursuit"] for item in payload["items"]}
    assert names == {"won-deal.md"}
    assert not (tmp_path / "accounts" / "acme" / "archive" / "template.md").exists()
    assert (pursuits / "template.md").exists()  # left untouched


# ── 6. load_pursuit exception → stage="" → counted as skipped ──────────────


@pytest.mark.unit
def test_cli_all_closed_malformed_frontmatter_counts_as_skipped(tmp_path: Path) -> None:
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    _make_malformed_pursuit_file(pursuits, "broken")

    runner = CliRunner()
    with patch("fieldkit.commands.pursuit.archive_cmd._data_root", return_value=tmp_path):
        result = runner.invoke(cli, ["--account", "acme", "--all-closed", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["skipped"] == 1
    assert payload["archived"] == 0
    assert payload["errors"] == 0
    assert payload["items"] == []
    assert (pursuits / "broken.md").exists()  # left untouched, not archived


# ── 7. errors += 1 when _archive_one reports ok=False in the loop ──────────


@pytest.mark.unit
def test_cli_all_closed_archive_one_failure_counts_as_error(tmp_path: Path) -> None:
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    target = _make_pursuit_file(pursuits, "won-deal", stage="closed-won")

    fail_record = {
        "pursuit": "won-deal.md",
        "source": str(target),
        "dest": None,
        "outcome": "error",
        "ok": False,
    }

    runner = CliRunner()
    with (
        patch("fieldkit.commands.pursuit.archive_cmd._data_root", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.archive_cmd._archive_one", return_value=fail_record),
    ):
        result = runner.invoke(cli, ["--account", "acme", "--all-closed", "--json"])

    payload = json.loads(result.output)
    assert payload["errors"] == 1
    assert payload["archived"] == 0
    assert len(payload["items"]) == 1
    assert payload["items"][0]["ok"] is False
    assert payload["items"][0]["stage"] == "closed-won"


# ── 8. --all-closed --json exit-code gate, both directions ─────────────────


@pytest.mark.unit
def test_cli_all_closed_json_exit_code_zero_when_no_errors(tmp_path: Path) -> None:
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    _make_pursuit_file(pursuits, "won-deal", stage="closed-won")

    runner = CliRunner()
    with patch("fieldkit.commands.pursuit.archive_cmd._data_root", return_value=tmp_path):
        result = runner.invoke(cli, ["--account", "acme", "--all-closed", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output)["errors"] == 0


@pytest.mark.unit
def test_cli_all_closed_json_exit_code_one_when_errors(tmp_path: Path) -> None:
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    target = _make_pursuit_file(pursuits, "won-deal", stage="closed-won")

    fail_record = {
        "pursuit": "won-deal.md",
        "source": str(target),
        "dest": None,
        "outcome": "error",
        "ok": False,
    }

    runner = CliRunner()
    with (
        patch("fieldkit.commands.pursuit.archive_cmd._data_root", return_value=tmp_path),
        patch("fieldkit.commands.pursuit.archive_cmd._archive_one", return_value=fail_record),
    ):
        result = runner.invoke(cli, ["--account", "acme", "--all-closed", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.output)["errors"] == 1
