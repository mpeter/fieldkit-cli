"""Regression contracts for matching and one guarded Salesforce publication."""

from pathlib import Path

import pytest

from fieldkit.sf.frontmatter import parse_salesforce_frontmatter_payload, update_salesforce_frontmatter
from fieldkit.sf.sync import match_pursuit

pytestmark = pytest.mark.unit
OPPORTUNITY_ID = "006000000000AAA"


def pursuit(workspace: Path, *, identity: str = "006000000000AAB") -> Path:
    path = workspace / "accounts" / "acme-corp" / "pursuits" / "expansion.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nstage: discover\ngate-status: pending\nsf_opportunity_id: {identity}\n---\n"
        f"\n{OPPORTUNITY_ID} is mentioned in the body.\n"
        "\n## Key Fields\n\n| Field | Value |\n| ----- | ----- |\n| Stage | Discover |\n",
        encoding="utf-8",
    )
    return path


def test_matching_does_not_use_body_identity(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = pursuit(tmp_path)

    result = match_pursuit(path.parent, OPPORTUNITY_ID, workspace=tmp_path)

    assert result is None
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("dry_run", [False, True])
def test_table_update_is_part_of_validated_frontmatter_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    import fieldkit.sf.frontmatter as frontmatter

    path = pursuit(tmp_path, identity=OPPORTUNITY_ID)
    original = path.read_bytes()
    published: list[str] = []
    publish = frontmatter._publish_validated_update

    def capture(plan: frontmatter._PublicationPlan) -> None:
        published.append(plan.rendered)
        publish(plan)

    monkeypatch.setattr(frontmatter, "_publish_validated_update", capture)
    result = update_salesforce_frontmatter(
        path,
        workspace=tmp_path,
        payload=parse_salesforce_frontmatter_payload({"opportunity_id": OPPORTUNITY_ID, "stage": "Propose"}),
        dry_run=dry_run,
        expected_opportunity_id=OPPORTUNITY_ID,
    )

    assert result.written is not dry_run
    if dry_run:
        assert path.read_bytes() == original
        assert published == []
    else:
        assert len(published) == 1
        assert "| Stage | Propose" in published[0]
        assert "| Stage | Discover" not in path.read_text(encoding="utf-8")
