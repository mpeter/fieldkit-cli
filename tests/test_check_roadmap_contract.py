"""Tests for the public roadmap's deterministic structural contract."""

from pathlib import Path

from scripts import check_roadmap_contract


def test_current_roadmap_satisfies_the_public_ledger_contract() -> None:
    """Every public roadmap section and status has one explicit current form."""
    roadmap = Path(__file__).parents[1] / "ROADMAP.md"

    assert check_roadmap_contract.validate(roadmap) == ()


def test_roadmap_rejects_an_unknown_status(tmp_path: Path) -> None:
    """A status cannot become an unreviewed catch-all for unfinished work."""
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text(
        "## Release safety and future delivery\n\n| Status | Outcome |\n| --- | --- |\n| Maybe | Unclear |\n",
        encoding="utf-8",
    )

    assert "unknown status: Maybe" in check_roadmap_contract.validate(roadmap)
