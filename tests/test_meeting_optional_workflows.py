from pathlib import Path

import pytest

WORKFLOWS = Path("src/fieldkit/skills/meeting/ops")


@pytest.mark.unit
@pytest.mark.parametrize(
    "filename",
    ["account-pulse.md", "one-on-one.md", "stakeholder-map.md"],
)
def test_optional_meeting_workflows_fail_closed_and_do_not_silently_write(filename: str) -> None:
    content = (WORKFLOWS / filename).read_text(encoding="utf-8")

    assert "Source status" in content
    assert "unavailable" in content
    assert "Present the draft for review" in content
    assert "Do not write" in content


@pytest.mark.unit
@pytest.mark.parametrize(
    "unsupported_claim",
    [
        "Groups needed:",
        "fieldkit-sales",
        "slackcli",
        "tvly search",
        "run `/brief` first",
        "use all available",
        "Saved to:",
    ],
)
def test_optional_meeting_workflows_do_not_require_private_routes(unsupported_claim: str) -> None:
    content = "\n".join(
        (WORKFLOWS / filename).read_text(encoding="utf-8")
        for filename in ("account-pulse.md", "one-on-one.md", "stakeholder-map.md")
    )

    assert unsupported_claim not in content


@pytest.mark.unit
def test_optional_meeting_workflows_use_shipped_read_only_commands() -> None:
    account_pulse = (WORKFLOWS / "account-pulse.md").read_text(encoding="utf-8")
    one_on_one = (WORKFLOWS / "one-on-one.md").read_text(encoding="utf-8")
    stakeholder_map = (WORKFLOWS / "stakeholder-map.md").read_text(encoding="utf-8")

    assert "fieldkit pursuit projects --account <account> --json" in account_pulse
    assert "fieldkit pursuit health --account <account> --json" in account_pulse
    assert "fieldkit gmail query blindspots <account> --since <YYYY-MM-DD> --limit 10 --json" in account_pulse
    assert "fieldkit gmail backstory-gap --account <account> --limit 10 --json" in account_pulse
    assert "fieldkit pursuit forecast --json" in one_on_one
    assert "fieldkit pursuit health --json" in one_on_one
    assert "fieldkit contact find <email> --affiliations --json" in stakeholder_map
    assert "fieldkit gmail query account <account> --since <YYYY-MM-DD> --limit 10 --json" in stakeholder_map


@pytest.mark.unit
def test_account_snapshot_distinguishes_decay_result_and_work_bounds() -> None:
    account_snapshot = (WORKFLOWS / "account-snapshot.md").read_text(encoding="utf-8")
    assert account_snapshot.startswith("# Account Snapshot\n")
    normalized = " ".join(account_snapshot.split())

    assert "threads tagged to the selected account" in normalized
    assert "`truncated: true`" in normalized
    assert "`scan_truncated: true`" in normalized
    assert "exits `1`" in normalized
    assert "empty incomplete report" in normalized
