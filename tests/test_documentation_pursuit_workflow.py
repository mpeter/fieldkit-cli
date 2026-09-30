"""Execute the pursuit guide's local output contracts without provider access."""

import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from fieldkit.commands.datasync.cli import RunConfig, StepResult, _build_steps, _run_pipeline_steps
from fieldkit.commands.pursuit import audit_cmd
from fieldkit.commands.pursuit.advance_cmd import cli as advance
from fieldkit.pursuit.gate_criteria import evaluate_gate_policy

pytestmark = pytest.mark.unit

_SYNC_ACCOUNT_CLAIM = (
    "- `--account SLUG` — scope people-index rebuilding, Gmail account tagging and enrichment, and selected watchers "
    "to one account; it does not scope Gmail sync, transcript discovery or processing, or the optional Salesforce listview step"
)


def _assert_documentation_sync_scope(document: str) -> None:
    step = document.split("## Step 1: Sync your data\n", 1)[1].split("## Step 2: Audit pursuits\n", 1)[0]
    items = re.findall(r"^- .*?(?=\n- |\n\n|\Z)", step, flags=re.MULTILINE | re.DOTALL)
    assert _SYNC_ACCOUNT_CLAIM in {" ".join(item.split()) for item in items}, "Unapproved sync account semantics"


def test_documentation_sync_account_scope_matches_selected_steps() -> None:
    _assert_documentation_sync_scope(Path("docs/guides/pipeline-workflow.md").read_text(encoding="utf-8"))
    planned = _build_steps(RunConfig(google=True, backstory=True, slack=True, sf=True, account="acme-corp"))
    assert planned
    steps = dict(planned)

    assert set(steps) == {
        "gmail sync",
        "people-index",
        "account-tags",
        "enrich-pursuits",
        "ingest discover",
        "ingest run",
        "backstory-health",
        "pursuit-stalls",
        "slack-threads",
        "sf listview",
    }
    assert steps["people-index"] == []
    scoped = {label for label, argv in steps.items() if "--account" in argv}
    assert scoped == {"account-tags", "enrich-pursuits", "backstory-health", "pursuit-stalls", "slack-threads"}
    for label in scoped:
        assert steps[label][-2:] == ["--account", "acme-corp"]
    assert {label for label, argv in steps.items() if argv and "--account" not in argv} == {
        "gmail sync",
        "ingest discover",
        "ingest run",
        "sf listview",
    }


@pytest.mark.parametrize("account", [None, "acme-corp"])
def test_documentation_sync_forwards_account_to_in_process_people_index(
    monkeypatch: pytest.MonkeyPatch, account: str | None
) -> None:
    observed: list[str | None] = []
    expected = StepResult(index=1, total=1, label="people-index", cmd=[], success=True, elapsed=0.0)

    def people_index(index: int, total: int, selected: str | None, *, dry_run: bool) -> StepResult:
        assert (index, total, dry_run) == (1, 1, False)
        observed.append(selected)
        return expected

    monkeypatch.setattr("fieldkit.commands.datasync.cli._run_people_index_step", people_index)

    results = _run_pipeline_steps(RunConfig(account=account, as_json=True), [("people-index", [])])

    assert results == [expected]
    assert observed == [account]


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("it does not scope Gmail sync", "it scopes Gmail sync"),
        ("transcript discovery or processing", "transcript discovery only"),
        ("or the optional Salesforce listview step", "except the optional Salesforce listview step"),
        ("scope people-index rebuilding", "does not scope people-index rebuilding"),
        ("- `--account SLUG`", "- It is false that `--account SLUG`"),
    ],
)
def test_documentation_sync_rejects_false_account_scope(before: str, after: str) -> None:
    document = "\n\n".join(
        " ".join(paragraph.split()) if not paragraph.startswith("- ") else paragraph
        for paragraph in Path("docs/guides/pipeline-workflow.md").read_text(encoding="utf-8").split("\n\n")
    )
    assert before in document
    with pytest.raises(AssertionError, match="sync account semantics"):
        _assert_documentation_sync_scope(document.replace(before, after))


_USER_GUIDE_CLAIMS = (
    "For configured Salesforce opportunities, fieldkit can read the native ClosePlan scorecard. "
    "Historical local MEDDPICC data is preserved as `legacy_meddpicc`, but it is not treated as current qualification. "
    "A stage transition whose policy depended on the former local scores reports `pending` until its native "
    "qualification policy is ratified. Fetching a current scorecard does not remove that policy hold, "
    "and historical data cannot authorize the transition.",
    "`sf meddpicc` requires configured Salesforce access. `pursuit audit` reads local pursuit files and writes a report "
    "under the workspace audit directory. The advance command is a non-writing local preview because it includes `--dry-run`; "
    "the relevant transitions remain pending until their policy is ratified. A pending preview exits `1` and does not advance. "
    "An explicit `--override REASON` can override that gate; omit `--dry-run` only when you intend to write the transition and its rationale.",
)


def _assert_user_guide_policy(document: str) -> None:
    normalized = {" ".join(paragraph.split()) for paragraph in document.split("\n\n")}
    for claim in _USER_GUIDE_CLAIMS:
        assert claim in normalized, f"Unapproved pursuit policy semantics: {claim}"


def test_documentation_user_guide_rejects_prefixed_policy_negation() -> None:
    document = "\n\n".join(
        " ".join(part.split()) for part in Path("docs/user-guide.md").read_text(encoding="utf-8").split("\n\n")
    )
    with pytest.raises(AssertionError, match="policy semantics"):
        _assert_user_guide_policy(document.replace(_USER_GUIDE_CLAIMS[0], "It is false that " + _USER_GUIDE_CLAIMS[0]))


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("reports `pending` until", "does not report `pending` until"),
        ("does not remove that policy hold", "does remove that policy hold"),
        ("historical data cannot authorize", "historical data can authorize"),
        ("exits `1` and does not advance", "exits `0` and does advance"),
        ("`--override REASON` can override", "`--override` can override"),
    ],
)
def test_documentation_user_guide_rejects_false_pursuit_policy(before: str, after: str) -> None:
    document = "\n\n".join(
        " ".join(part.split()) for part in Path("docs/user-guide.md").read_text(encoding="utf-8").split("\n\n")
    )
    assert before in document
    with pytest.raises(AssertionError, match="policy semantics"):
        _assert_user_guide_policy(document.replace(before, after))


@pytest.fixture(autouse=True)
def _confine_advance_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "fieldkit.commands.pursuit.advance_cmd.get_accounts_root",
        lambda: tmp_path / "accounts",
    )


@pytest.fixture
def pursuit(tmp_path: Path) -> Path:
    path = tmp_path / "accounts" / "acme-corp" / "pursuits" / "acme-corp-q3.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nstage: discover\ngate-status: pending\nlast-transition: null\ntransition-history: []\n---\n",
        encoding="utf-8",
    )
    return path


def _section_blocks(heading: str, next_heading: str) -> list[str]:
    document = Path("docs/guides/pipeline-workflow.md").read_text(encoding="utf-8")
    section = document.split(heading + "\n", 1)[1].split(next_heading + "\n", 1)[0]
    blocks = re.findall(r"```[^\n]*\n(.*?)```", section, flags=re.DOTALL)
    assert len(blocks) == 2
    return blocks


def test_documentation_audit_output_and_default_report(
    tmp_path: Path, pursuit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocks = _section_blocks("## Step 2: Audit pursuits", "## Step 3: Advance a pursuit")
    assert blocks[0].strip() == "fieldkit pursuit audit"
    before = pursuit.read_bytes()
    monkeypatch.setattr(audit_cmd, "get_fieldkit_home", lambda: tmp_path)

    result = CliRunner().invoke(audit_cmd.cli, [])

    assert result.exit_code == 0, result.output
    summary, separator, report_location = result.output.partition("\nReport written to: ")
    assert separator
    assert summary.strip("\n") == blocks[1].strip("\n")
    reports = list((tmp_path / "accounts" / ".audit").glob("pursuit-compliance-*.md"))
    assert len(reports) == 1
    assert report_location.strip() == reports[0].relative_to(tmp_path).as_posix()
    assert str(tmp_path) not in result.output
    assert "current qualification: unavailable" in reports[0].read_text(encoding="utf-8")
    assert pursuit.read_bytes() == before


def test_documentation_advance_preview_is_pending_without_writes(tmp_path: Path, pursuit: Path) -> None:
    _assert_user_guide_policy(Path("docs/user-guide.md").read_text(encoding="utf-8"))
    decision = evaluate_gate_policy("discover", "validate")
    assert decision.status == "pending"
    assert not decision.passed
    blocks = _section_blocks("## Step 3: Advance a pursuit", "## Step 4: Forecast")
    assert blocks[0].strip() == "fieldkit pursuit advance PURSUIT_SPEC --dry-run"
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    result = CliRunner().invoke(advance, [str(pursuit), "--dry-run"])

    assert result.exit_code == 1, result.output
    assert result.output.split("\n", 2)[2].rstrip("\n") == blocks[1].rstrip("\n")
    after = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before
