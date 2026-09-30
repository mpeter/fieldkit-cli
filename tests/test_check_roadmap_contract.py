"""Tests for the public roadmap's deterministic structural contract."""

from pathlib import Path

import pytest

from scripts import check_roadmap_contract
from scripts.markdown_tables import MAX_DOCUMENT_BYTES

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parents[1]


def test_current_roadmap_satisfies_the_public_ledger_contract() -> None:
    """Every public roadmap section and status has one explicit current form."""
    roadmap = _ROOT / "ROADMAP.md"

    assert check_roadmap_contract.validate(roadmap) == ()


@pytest.mark.parametrize("extra_row", [False, True])
def test_release_safety_table_requires_the_complete_reviewed_inventory(tmp_path: Path, extra_row: bool) -> None:
    content = (_ROOT / "ROADMAP.md").read_text(encoding="utf-8")
    row = "| Pending | Verify credential-file permission controls on every supported platform. |"
    assert row in content
    replacement = row + "\n| Pending | Unreviewed release outcome |" if extra_row else ""
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text(content.replace(row, replacement), encoding="utf-8")

    findings = check_roadmap_contract.validate(roadmap)

    expected_count = 14 if extra_row else 12
    assert f"Release safety and future delivery: expected 13 rows, found {expected_count}" in findings


def test_roadmap_rejects_an_unknown_status(tmp_path: Path) -> None:
    """A status cannot become an unreviewed catch-all for unfinished work."""
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text(
        "# fieldkit roadmap\n\n## Release safety and future delivery\n\n"
        "| Status | Outcome |\n| --- | --- |\n| Maybe | Unclear |\n",
        encoding="utf-8",
    )

    assert "unknown status" in check_roadmap_contract.validate(roadmap)


@pytest.mark.parametrize(
    ("original", "replacement", "diagnostic"),
    [
        ("| Status | Outcome |\n| --- | --- |\n", "", "requires exactly one table"),
        ("| Status | Outcome |", "| Status | Outcome | Extra |", "malformed roadmap table"),
        (
            "## Release safety and future delivery",
            "## Release safety and future delivery\n\n## Release safety and future delivery",
            "sections must be unique and in the reviewed order",
        ),
        (
            "## Product reliability and integrations",
            "In progress | This ignored work must not vanish |\n\n## Product reliability and integrations",
            "unowned section content",
        ),
        (
            "| Pending | Verify credential-file permission controls on every supported platform. |",
            "| Pending | |",
            "roadmap outcome must be nonempty",
        ),
    ],
)
def test_roadmap_rejects_unowned_or_malformed_structure(
    tmp_path: Path, original: str, replacement: str, diagnostic: str
) -> None:
    content = (_ROOT / "ROADMAP.md").read_text(encoding="utf-8")
    assert original in content
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text(content.replace(original, replacement), encoding="utf-8")

    findings = check_roadmap_contract.validate(roadmap)

    assert any(diagnostic in finding for finding in findings)


def test_roadmap_rejects_duplicate_outcomes(tmp_path: Path) -> None:
    lines = (_ROOT / "ROADMAP.md").read_text(encoding="utf-8").splitlines()
    rows = [index for index, line in enumerate(lines) if line.startswith("| In progress |")]
    lines[rows[1]] = lines[rows[0]]
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text("\n".join(lines) + "\n", encoding="utf-8")

    findings = check_roadmap_contract.validate(roadmap)

    assert any("duplicate outcome" in finding for finding in findings)


@pytest.mark.parametrize(
    "outcome",
    ["<!-- hidden -->", "<span hidden>concealed plan</span>", "&nbsp;", "[](#)", "\u200b", "&#8203;", "&shy;"],
)
def test_roadmap_rejects_hidden_or_rendered_empty_outcomes(tmp_path: Path, outcome: str) -> None:
    content = (_ROOT / "ROADMAP.md").read_text(encoding="utf-8")
    original = "Verify credential-file permission controls on every supported platform."
    assert original in content
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text(content.replace(original, outcome), encoding="utf-8")

    findings = check_roadmap_contract.validate(roadmap)

    assert findings
    assert any("outcome" in finding for finding in findings)


def test_roadmap_rejects_a_table_without_a_section_owner(tmp_path: Path) -> None:
    content = (_ROOT / "ROADMAP.md").read_text(encoding="utf-8")
    extra = "| Status | Outcome |\n| --- | --- |\n| Pending | Unowned work |\n\n"
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text(content.replace("## Release", extra + "## Release", 1), encoding="utf-8")

    findings = check_roadmap_contract.validate(roadmap)

    assert "unowned roadmap table" in findings


@pytest.mark.parametrize("delimiter", ["```", "~~~", "<!--"])
def test_roadmap_cannot_hide_its_plan_in_code_or_html(tmp_path: Path, delimiter: str) -> None:
    content = (_ROOT / "ROADMAP.md").read_text(encoding="utf-8")
    closing = "-->" if delimiter == "<!--" else delimiter
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text(content.replace("## Release", delimiter + "\n## Release", 1) + closing + "\n", encoding="utf-8")

    findings = check_roadmap_contract.validate(roadmap)

    assert "roadmap must not hide structured content in code or HTML" in findings


@pytest.mark.parametrize("source", [b"\xff", b"x" * (MAX_DOCUMENT_BYTES + 1)])
def test_roadmap_read_failures_are_nonpassing_and_payload_free(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], source: bytes
) -> None:
    roadmap = tmp_path / "unapproved-source.md"
    roadmap.write_bytes(source)

    result = check_roadmap_contract.main(["--roadmap", str(roadmap)])

    assert result == 2
    captured = capsys.readouterr()
    assert "unable to read a bounded UTF-8 roadmap" in captured.err
    assert str(roadmap) not in captured.err
    assert len(captured.err) < 128


def test_roadmap_rejects_symlink_sources(tmp_path: Path) -> None:
    source = tmp_path / "actual.md"
    source.write_text((_ROOT / "ROADMAP.md").read_text(encoding="utf-8"), encoding="utf-8")
    link = tmp_path / "ROADMAP.md"
    link.symlink_to(source)

    with pytest.raises(ValueError, match="must not use symlinks"):
        check_roadmap_contract.validate(link)


def test_missing_roadmap_has_a_payload_free_nonpassing_result(tmp_path: Path) -> None:
    result = check_roadmap_contract.validate(tmp_path / "missing.md")

    assert result == ("missing roadmap",)
