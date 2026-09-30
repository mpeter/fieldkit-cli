"""Behavioral contracts for the remaining public skill instruction surfaces."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from itertools import pairwise
from pathlib import Path
from typing import get_args, get_type_hints

import pytest
import yaml

import fieldkit.config as config
import fieldkit.config._loader as config_loader
from fieldkit.__main__ import main
from fieldkit.config import clear_config_caches
from fieldkit.pursuit import parse_frontmatter
from fieldkit.pursuit.enums import Stage
from fieldkit.pursuit.gate_criteria import (
    NATIVE_QUALIFICATION_PENDING_REASON,
    PENDING_NATIVE_QUALIFICATION_TRANSITIONS,
)
from fieldkit.pursuit.paths import PursuitPathError, resolve_pursuit_file
from fieldkit.pursuit.stages import PIPELINE_STAGES
from fieldkit.sf.types import MeddpiccReadResult
from scripts.check_documentation_contract import _has_future_status, fenced_blocks
from scripts.markdown_tables import markdown_tables
from tests.documentation_workflow_support import snapshot_workflow

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COMPANION = _REPO_ROOT / "src/fieldkit/skills/companion/SKILL.md"
_GRILL = _REPO_ROOT / "src/fieldkit/skills/grill/SKILL.md"
_ADVANCE = _REPO_ROOT / "src/fieldkit/skills/pursuit-advance/SKILL.md"
_GATE_REFERENCE = _REPO_ROOT / "src/fieldkit/skills/pursuit-advance/gate-reference.md"
_NARRATIVE = _REPO_ROOT / "src/fieldkit/_data/pursuit-narrative-template.md"


@pytest.mark.parametrize("page", (_COMPANION, _GRILL, _ADVANCE, _GATE_REFERENCE, _NARRATIVE))
def test_remaining_pages_do_not_embed_project_future_work(page: Path) -> None:
    relative = page.relative_to(_REPO_ROOT).as_posix()

    assert _has_future_status(relative, page.read_text(encoding="utf-8")) is False


@dataclass(frozen=True)
class _Execution:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class _BlockContract:
    page: Path
    index: int
    language: str
    owner: str


@dataclass(frozen=True)
class _InlineContract:
    page: Path
    command: str
    owner: str


@dataclass(frozen=True)
class _TableContract:
    page: Path
    index: int
    owner: str


_BLOCK_CONTRACTS = (
    _BlockContract(_COMPANION, 1, "bash", "fixed.companion-feed"),
    _BlockContract(_COMPANION, 2, "bash", "fixed.companion-allowed"),
    _BlockContract(_COMPANION, 3, "bash", "fixed.companion-run"),
    _BlockContract(_GRILL, 1, "bash", "pending.credentialed-closeplan-read"),
    _BlockContract(_GRILL, 2, "bash", "pending.credentialed-closeplan-write"),
    _BlockContract(_GRILL, 3, "text", "structural.pursuit-discovery"),
    _BlockContract(_GRILL, 4, "text", "structural.pipeline-output"),
    _BlockContract(_GRILL, 5, "text", "structural.project-discovery"),
    _BlockContract(_ADVANCE, 1, "", "structural.stage-sequence"),
    _BlockContract(_ADVANCE, 2, "bash", "fixed.pursuit-preview"),
    _BlockContract(_ADVANCE, 3, "", "structural.pass-advisory"),
    _BlockContract(_ADVANCE, 4, "", "structural.pending-advisory"),
    _BlockContract(_ADVANCE, 5, "bash", "fixed.pursuit-writes"),
    _BlockContract(_ADVANCE, 6, "markdown", "structural.success-output"),
)

_SAFE_FENCE_BODIES = {
    "fixed.companion-feed": "fieldkit companion feed --json --all",
    "fixed.companion-allowed": (
        "fieldkit companion allowed -- pursuit advance acme-corp/deal --dry-run\n"
        "# exit 0 = permitted, exit 3 = denied (permanent — do not retry, do not work around)"
    ),
    "fixed.companion-run": ("fieldkit companion run --item-id <item_id> -- pursuit advance acme-corp/deal --dry-run"),
    "fixed.pursuit-preview": ("fieldkit pursuit advance <account>/<pursuit> --to <target-stage> --dry-run --json"),
    "fixed.pursuit-writes": (
        "# Passing qualification-independent transition\n"
        "fieldkit pursuit advance <account>/<pursuit> --to <target-stage> --json\n\n"
        "# Pending transition that the operator explicitly overrides\n"
        "fieldkit pursuit advance <account>/<pursuit> --to <target-stage> --override '<reason>' --json"
    ),
}

_INLINE_CONTRACTS = (
    _InlineContract(_GRILL, "uv run fieldkit", "structural.worktree-invocation"),
    _InlineContract(
        _GRILL,
        "fieldkit sf meddpicc <opp_id> --deal-id <closeplan_deal_id> --json",
        "pending.credentialed-closeplan-selection",
    ),
    _InlineContract(_GRILL, "fieldkit sf update-closeplan", "structural.closeplan-command-name"),
    _InlineContract(_ADVANCE, "fieldkit pursuit advance", "structural.advance-command-name"),
    _InlineContract(
        _NARRATIVE,
        "fieldkit sf meddpicc <opp_id> --json",
        "pending.credentialed-narrative-closeplan-read",
    ),
)

_TABLE_CONTRACTS = (
    _TableContract(_GRILL, 1, "structural.native-reader-statuses"),
    _TableContract(_GRILL, 2, "structural.native-question-template"),
    _TableContract(_GRILL, 3, "structural.local-field-sources"),
    _TableContract(_GRILL, 4, "structural.pipeline-output"),
    _TableContract(_GRILL, 5, "structural.next-action-policy"),
    _TableContract(_ADVANCE, 1, "structural.transition-policy"),
    _TableContract(_NARRATIVE, 1, "structural.narrative-key-fields"),
    _TableContract(_NARRATIVE, 2, "structural.narrative-stakeholders"),
    _TableContract(_NARRATIVE, 3, "structural.narrative-risks"),
    _TableContract(_NARRATIVE, 4, "structural.narrative-scope"),
)


def _invoke(argv: list[str]) -> _Execution:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(argv)
    return _Execution(exit_code, stdout.getvalue(), stderr.getvalue())


def _changed_paths(before: Mapping[str, object], after: Mapping[str, object]) -> set[str]:
    missing = object()
    return {name for name in before.keys() | after.keys() if before.get(name, missing) != after.get(name, missing)}


def _fenced_blocks(path: Path) -> list[tuple[str, str]]:
    return [(block.language, block.body.rstrip("\r\n")) for block in fenced_blocks(path)]


def _assert_fenced_blocks_owned(pages: tuple[Path, ...], contracts: tuple[_BlockContract, ...]) -> None:
    actual = {(contract.page, contract.index): (contract.language, contract.owner) for contract in contracts}
    assert len(actual) == len(contracts)

    discovered: set[tuple[Path, int]] = set()
    for page in pages:
        for index, (language, _body) in enumerate(_fenced_blocks(page), start=1):
            key = (page, index)
            discovered.add(key)
            assert key in actual, f"unowned fenced block: {page}:{index}"
            assert actual[key][0] == language
    assert discovered == set(actual)


def _write_pursuit(path: Path, *, stage: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"stage: {stage}\n"
        "gate-status: pending\n"
        "last-transition: 2026-01-01\n"
        "transition-history: []\n"
        "---\n"
        "# Fictional deal\n",
        encoding="utf-8",
    )


@pytest.fixture
def isolated_skill_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deny_documentation_network: None,
) -> Iterator[tuple[Path, Path, Path, Path]]:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    normal_home = tmp_path / "normal-home"
    xdg_config = tmp_path / "xdg-config"
    workspace.mkdir()
    runtime.mkdir()
    normal_home.mkdir()
    config_path = xdg_config / "fieldkit" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        yaml.safe_dump(
            {
                "fieldkit_home": str(workspace),
                "fieldkit_data": str(runtime),
                "companion": {
                    "tier": "act",
                    "act_allowlist": ["pursuit advance acme-corp/deal --dry-run"],
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    _write_pursuit(workspace / "accounts/acme-corp/pursuits/deal.md", stage="pre-pipeline")
    _write_pursuit(workspace / "accounts/acme-corp/pursuits/pending-deal.md", stage="discover")
    monkeypatch.setenv("HOME", str(normal_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(runtime))
    monkeypatch.setenv("FIELDKIT_NO_LLM", "1")
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    clear_config_caches()
    yield tmp_path, workspace, runtime, normal_home
    clear_config_caches()


def test_every_fenced_block_has_one_fail_closed_owner() -> None:
    _assert_fenced_blocks_owned(
        (_COMPANION, _GRILL, _ADVANCE, _GATE_REFERENCE, _NARRATIVE),
        _BLOCK_CONTRACTS,
    )


def test_commonmark_container_and_alternate_fences_cannot_hide_unreviewed_commands(tmp_path: Path) -> None:
    page = tmp_path / "unreviewed.md"
    page.write_text(
        "~~~bash\nfieldkit version\n~~~\n\n"
        "````console\nfieldkit --help\n````\n\n"
        "- ```bash\n  fieldkit doctor\n  ```\n\n"
        "> ```bash\n> fieldkit skill list\n> ```\n",
        encoding="utf-8",
    )

    assert _fenced_blocks(page) == [
        ("bash", "fieldkit version"),
        ("console", "fieldkit --help"),
        ("bash", "fieldkit doctor"),
        ("bash", "fieldkit skill list"),
    ]
    with pytest.raises(AssertionError, match="unowned fenced block"):
        _assert_fenced_blocks_owned((page,), ())


def test_credentialed_examples_remain_explicitly_pending() -> None:
    pending = [contract for contract in _BLOCK_CONTRACTS if contract.owner.startswith("pending.")]
    bodies = [_fenced_blocks(contract.page)[contract.index - 1][1] for contract in pending]

    assert len(pending) == 2
    assert bodies == [
        "fieldkit sf meddpicc <opp_id> --json",
        "fieldkit sf update-closeplan <opp_id> --deal-id <closeplan_deal_id> \\\n  --score <question_id>=<native_score>",
    ]
    assert all("<" in body and ">" in body for body in bodies)

    pending_inline = [contract for contract in _INLINE_CONTRACTS if contract.owner.startswith("pending.")]
    assert len(pending_inline) == 2
    assert all("fieldkit " in contract.command for contract in pending_inline)


def _inline_commands(text: str) -> list[str]:
    return re.findall(r"`((?:uv run )?fieldkit(?:[ \t][^`\n]*)?)`", text)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("`fieldkit.pursuit.stages`", []),
        ("`fieldkit.pursuit.gate_criteria`", []),
        ("`fieldkit`", ["fieldkit"]),
        ("`fieldkit unknown --json`", ["fieldkit unknown --json"]),
        ("`uv run fieldkit unknown`", ["uv run fieldkit unknown"]),
    ],
)
def test_inline_command_discovery_distinguishes_module_lookups(text: str, expected: list[str]) -> None:
    assert _inline_commands(text) == expected


def test_every_inline_command_reference_has_one_fail_closed_owner() -> None:
    expected = {(contract.page, contract.command): contract.owner for contract in _INLINE_CONTRACTS}
    assert len(expected) == len(_INLINE_CONTRACTS)
    discovered: set[tuple[Path, str]] = set()

    for page in (_COMPANION, _GRILL, _ADVANCE, _GATE_REFERENCE, _NARRATIVE):
        for command in _inline_commands(page.read_text(encoding="utf-8")):
            key = (page, command)
            discovered.add(key)
            assert key in expected, f"unowned inline command reference: {page}: {command}"

    assert discovered == set(expected)


def test_safe_fences_remain_bound_to_executed_scenarios() -> None:
    safe_contracts = [contract for contract in _BLOCK_CONTRACTS if contract.owner.startswith("fixed.")]

    assert {contract.owner for contract in safe_contracts} == set(_SAFE_FENCE_BODIES)
    for contract in safe_contracts:
        body = _fenced_blocks(contract.page)[contract.index - 1][1]
        assert body == _SAFE_FENCE_BODIES[contract.owner]


def test_every_table_has_one_fail_closed_semantic_owner() -> None:
    pages = (_COMPANION, _GRILL, _ADVANCE, _GATE_REFERENCE, _NARRATIVE)
    owners = {(contract.page, contract.index): contract.owner for contract in _TABLE_CONTRACTS}
    assert len(owners) == len(_TABLE_CONTRACTS)

    discovered = {(page, index) for page in pages for index, _table in enumerate(markdown_tables(page), start=1)}

    assert discovered == set(owners)


def test_structural_table_parser_rejects_malformed_or_unbounded_templates(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.md"
    malformed.write_text("| A | B |\n|---|---|\n| only one |\n", encoding="utf-8")
    with pytest.raises(ValueError, match="column count"):
        markdown_tables(malformed)

    unbounded = tmp_path / "unbounded.md"
    unbounded.write_text("| A |\n|---|\n" + "| row |\n" * 65, encoding="utf-8")
    with pytest.raises(ValueError, match="row bound"):
        markdown_tables(unbounded)


def test_grill_tables_match_native_closeplan_and_pipeline_contracts() -> None:
    tables = markdown_tables(_GRILL)

    assert len(tables) == 5
    reader, question, local_fields, pipeline, actions = tables
    assert reader.header == ("Reader result", "Native Qualification", "Required response")
    statuses = set(get_args(get_type_hints(MeddpiccReadResult)["status"]))
    documented_statuses = {
        token
        for row in reader.rows
        for token in re.findall(r"`(complete|not_found|ambiguous|invalid_selection|incomplete)`", row[0])
    }
    assert documented_statuses == statuses
    assert tuple(row[1] for row in reader.rows) == (
        "`read`",
        "`pending`",
        "`pending`",
        "`unavailable`",
        "`unavailable`",
        "`unavailable`",
    )

    assert question.header == (
        "Exact question",
        "Question ID",
        "Current native value",
        "Package choices / maxima",
        "Metadata status",
        "Staged evidence",
        "Evidence needed",
    )
    assert question.rows == ()

    assert local_fields.header == ("Field", "Source", "Fallback")
    assert tuple(row[0] for row in local_fields.rows) == (
        "Deal name",
        "Account",
        "Stage",
        "Gate",
        "Last transition",
        "Opportunity ID",
        "SF stage/close date/last pulled",
    )
    assert local_fields.rows[3][2] == "`pending`"
    assert local_fields.rows[5][2] == "none; native status is unavailable"

    assert pipeline.header == (
        "Deal",
        "Account",
        "Stage",
        "Gate",
        "Days",
        "Native Qualification",
        "SF Stage",
        "SF Close Date",
        "Evidence Need",
        "Next Action",
    )
    assert len(pipeline.rows) == 1
    assert pipeline.rows[0][5] == "[read/pending/unavailable + reason]"
    assert pipeline.rows[0][8] == "[exact question ID or reason]"

    assert actions.header == ("Condition", "Next action")
    assert tuple(row[0] for row in actions.rows) == (
        "No Opportunity ID",
        "Authentication/read failure",
        "Several linked deals",
        "Incomplete collection",
        "Missing question metadata",
        "Exact unanswered question",
        "Explicit override",
        "Complete observed questions",
    )


def test_pursuit_advance_structures_derive_from_canonical_policy() -> None:
    blocks = _fenced_blocks(_ADVANCE)
    sequence = " → ".join((*PIPELINE_STAGES, Stage.CLOSED_WON))
    assert blocks[0] == ("", sequence)
    assert blocks[2] == (
        "",
        "Current policy passes for [current-stage] → [target-stage]. Recommend advancing [deal].\n\n"
        "Confirm? (yes / no)",
    )
    assert blocks[3] == (
        "",
        "Current Salesforce-native qualification policy is pending for this transition.\n\n"
        "Options:\n"
        "  1. Stop without changing the pursuit\n"
        "  2. Override — proceed with a documented reason\n\n"
        'If override: provide a brief reason (e.g. "EB meeting scheduled for Friday,\n'
        'advancing to keep proposal timeline on track").',
    )
    advance_text = _ADVANCE.read_text(encoding="utf-8")
    assert "**If the preview is PENDING:**" in advance_text
    assert "Present the actual `reasons` from the preview" in advance_text
    assert (
        "For a backward transition, report\n`Backward transitions require an explicit override reason`" in advance_text
    )
    assert blocks[5] == (
        "markdown",
        "## Stage Updated\n\n"
        "**Deal:** [opportunity name]\n"
        "**Account:** [account]\n"
        "**Transition:** [from] → [to]\n"
        "**Policy result:** [pass / override]\n"
        "[If override] **Override reason:** [reason]\n"
        "**Date:** [today]\n\n"
        "Next steps for [target-stage]:\n"
        "[Optional operator-reviewed actions grounded in the selected pursuit]",
    )

    policy = markdown_tables(_ADVANCE)
    assert len(policy) == 1
    assert policy[0].header == ("Transition", "Current policy result")
    pending_rows = tuple(
        (
            f"{source} → {target}",
            f"`pending` — {NATIVE_QUALIFICATION_PENDING_REASON.removesuffix(' for this transition')}",
        )
        for source, target in pairwise(PIPELINE_STAGES)
        if (source, target) in PENDING_NATIVE_QUALIFICATION_TRANSITIONS
    )
    assert policy[0].rows[:3] == pending_rows
    assert policy[0].rows[3:] == (
        ("negotiate → closed-won", "`pass` — qualification-independent transition"),
        ("Other forward transitions", "`pass` — qualification-independent transition"),
    )

    gate_reference = _GATE_REFERENCE.read_text(encoding="utf-8")
    documented_json_fields = re.search(r"The preview includes ([^.]+)\.", gate_reference)
    assert documented_json_fields is not None
    assert set(re.findall(r"`([a-z_]+)`", documented_json_fields.group(1))) == {
        "from_stage",
        "to_stage",
        "gate_passed",
        "gate_status",
        "reasons",
        "override",
        "advanced",
        "dry_run",
        "note",
    }


def test_narrative_template_tables_keep_reviewed_row_semantics() -> None:
    tables = markdown_tables(_NARRATIVE)

    assert len(tables) == 4
    key_fields, stakeholders, risks, scope = tables
    assert key_fields.header == ("Field", "Value")
    assert tuple(row[0] for row in key_fields.rows) == (
        "Stage",
        "Close Date",
        "ACV",
        "SKU",
        "SOW Period",
        "Type",
        "Budget Cap",
    )
    assert stakeholders.header == ("Contact", "Signal")
    roles = []
    for row in stakeholders.rows:
        match = re.match(r"\*\*([^*]+)\*\*", row[1])
        assert match is not None
        roles.append(match.group(1))
    assert tuple(roles) == ("Champion", "Economic Buyer", "Influencer", "Internal")
    assert risks.header == ("Risk", "Severity", "Mitigation")
    assert tuple(row[1] for row in risks.rows) == ("HIGH", "MEDIUM", "LOW")
    assert scope.header == ("Role", "Hours", "Rate", "Total")
    assert tuple(row[0] for row in scope.rows) == (
        "[Role, e.g. Delivery Lead]",
        "[Role, e.g. Sr. Architect]",
        "[Role, e.g. Sr. Consultant]",
        "[Role, e.g. Consultant]",
        "**Grand Total**",
    )
    assert scope.rows[-1] == ("**Grand Total**", "[hrs]", "", "**$[total]**")


def test_safe_read_and_preview_examples_execute_with_fixed_argv(
    isolated_skill_runtime: tuple[Path, Path, Path, Path],
) -> None:
    trial_root, _workspace, _runtime, _normal_home = isolated_skill_runtime
    before = snapshot_workflow(trial_root)

    scenarios = (
        (["companion", "feed", "--json", "--all"], 0),
        (["companion", "allowed", "--", "pursuit", "advance", "acme-corp/deal", "--dry-run"], 0),
        (["companion", "allowed", "--", "pursuit", "advance", "acme-corp/deal"], 3),
        (
            ["pursuit", "advance", "acme-corp/deal", "--to", "prospect", "--dry-run", "--json"],
            0,
        ),
        (
            ["pursuit", "advance", "acme-corp/pending-deal", "--to", "validate", "--dry-run", "--json"],
            1,
        ),
        (
            ["pursuit", "advance", "acme-corp/pending-deal", "--to", "qualify", "--dry-run", "--json"],
            1,
        ),
    )
    results = [(_invoke(argv), expected) for argv, expected in scenarios]

    assert [result.exit_code for result, _expected in results] == [expected for _result, expected in results]
    preview = json.loads(results[3][0].stdout)
    pending = json.loads(results[4][0].stdout)
    backward = json.loads(results[5][0].stdout)
    assert preview["gate_status"] == "pass" and preview["advanced"] is False
    assert pending["gate_status"] == "pending" and pending["advanced"] is False
    assert backward["gate_status"] == "pending" and backward["advanced"] is False
    assert backward["reasons"] == ["Backward transitions require an explicit override reason"]
    assert snapshot_workflow(trial_root) == before


def test_companion_run_executes_exact_preview_and_only_journals(
    isolated_skill_runtime: tuple[Path, Path, Path, Path],
) -> None:
    trial_root, workspace, runtime, _normal_home = isolated_skill_runtime
    pursuit = workspace / "accounts/acme-corp/pursuits/deal.md"
    before_pursuit = pursuit.read_bytes()
    before = snapshot_workflow(trial_root)

    result = _invoke(
        [
            "companion",
            "run",
            "--item-id",
            "fixture-item",
            "--",
            "pursuit",
            "advance",
            "acme-corp/deal",
            "--dry-run",
        ]
    )

    assert result.exit_code == 0, result.stderr
    assert "[dry-run] Would advance" in result.stdout
    assert pursuit.read_bytes() == before_pursuit
    journals = list(runtime.glob("companion-journal-*.jsonl"))
    assert len(journals) == 1
    records = [json.loads(line) for line in journals[0].read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["item_id"] == "fixture-item"
    assert records[0]["action"] == "pursuit advance acme-corp/deal --dry-run"
    assert records[0]["exit_code"] == 0
    changed = _changed_paths(before, snapshot_workflow(trial_root))
    assert changed == {"runtime", f"runtime/{journals[0].name}"}


def test_companion_instructions_disclose_unjournaled_preflight_refusals() -> None:
    text = _COMPANION.read_text(encoding="utf-8")

    assert "journal every outcome" not in text
    assert "malformed allowlist" in text
    assert "invalid action authority" in text
    assert "before journaling" in text


@pytest.mark.parametrize(
    ("argv", "pursuit_name", "expected_stage", "expected_gate"),
    [
        (
            ["pursuit", "advance", "acme-corp/deal", "--to", "prospect", "--json"],
            "deal.md",
            "prospect",
            "pass",
        ),
        (
            [
                "pursuit",
                "advance",
                "acme-corp/pending-deal",
                "--to",
                "validate",
                "--override",
                "Operator approved the exception",
                "--json",
            ],
            "pending-deal.md",
            "validate",
            "override",
        ),
    ],
)
def test_documented_stage_writes_execute_only_inside_fixture_workspace(
    isolated_skill_runtime: tuple[Path, Path, Path, Path],
    argv: list[str],
    pursuit_name: str,
    expected_stage: str,
    expected_gate: str,
) -> None:
    trial_root, workspace, _runtime, _normal_home = isolated_skill_runtime
    target = workspace / "accounts/acme-corp/pursuits" / pursuit_name
    before = snapshot_workflow(trial_root)

    result = _invoke(argv)
    parsed = parse_frontmatter(target.read_text(encoding="utf-8"))
    payload = json.loads(result.stdout)

    assert result.exit_code == 0, result.stderr
    assert payload["advanced"] is True
    assert payload["dry_run"] is False
    assert payload["to_stage"] == expected_stage
    assert payload["gate_status"] == expected_gate
    assert Path(payload["pursuit"]).resolve(strict=True) == target.resolve(strict=True)
    assert parsed is not None
    frontmatter, _body = parsed
    assert frontmatter["stage"] == expected_stage
    assert frontmatter["gate-status"] == expected_gate
    assert frontmatter["transition-history"][-1]["gate-result"] == expected_gate
    changed = _changed_paths(before, snapshot_workflow(trial_root))
    target_name = str(target.relative_to(trial_root))
    assert target_name in changed
    assert changed - {target_name} == {
        "runtime",
        "runtime/locks",
        "runtime/locks/pursuit",
        str(target.parent.relative_to(trial_root)),
        next(name for name in changed if re.fullmatch(r"runtime/locks/pursuit/[0-9a-f]{64}\.lock", name)),
    }


def test_instruction_pages_are_portable_and_truthful_about_boundaries() -> None:
    pages = [_COMPANION, _GRILL, _ADVANCE, _GATE_REFERENCE, _NARRATIVE]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in pages)

    assert not re.search(r"(?:^|[/`])\.(?:serena|claude|opencode)(?:[/`]|$)", combined, re.MULTILINE)
    assert "Red Hat" not in combined
    assert "HCS" not in combined
    assert "CU-GPS" not in combined
    assert "not an operating-system sandbox" in _COMPANION.read_text(encoding="utf-8")
    assert "confines both explicit paths" in _ADVANCE.read_text(encoding="utf-8")
    narrative = " ".join(_NARRATIVE.read_text(encoding="utf-8").split())
    assert "does not create a Google document" in narrative
    for expansion in (
        "Statement of Work (SOW)",
        "Salesforce (SF)",
        "annual contract value (ACV)",
        "stock keeping unit (SKU)",
        "proof of concept (POC)",
        "knowledge transfer (KT)",
    ):
        assert expansion in narrative


@pytest.mark.parametrize("redirect", [False, True], ids=["outside-file", "symlink-redirect"])
def test_pursuit_advance_rejects_paths_outside_the_workspace_before_reading(
    isolated_skill_runtime: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    redirect: bool,
) -> None:
    trial_root, workspace, _runtime, _normal_home = isolated_skill_runtime
    outside = tmp_path / "private-outside.md"
    outside.write_text("---\nstage: pre-pipeline\n---\nprivate marker\n", encoding="utf-8")
    selector = outside
    if redirect:
        selector = workspace / "accounts/acme-corp/pursuits/redirect.md"
        selector.symlink_to(outside)
    outside_before = outside.read_bytes()
    before = snapshot_workflow(trial_root)

    result = _invoke(["pursuit", "advance", str(selector), "--to", "prospect"])

    assert result.exit_code == 3
    assert "existing file in the configured workspace" in result.stderr
    assert str(outside) not in result.stderr
    assert "private marker" not in result.stderr
    assert outside.read_bytes() == outside_before
    assert snapshot_workflow(trial_root) == before


@pytest.mark.parametrize(
    "selector",
    [
        "accounts/acme-corp/pursuits/deal.md",
        "acme-corp/pursuits/deal.md",
        "acme-corp/deal",
    ],
)
def test_pursuit_resolution_accepts_supported_workspace_selectors(tmp_path: Path, selector: str) -> None:
    workspace = tmp_path / "workspace"
    pursuit = workspace / "accounts/acme-corp/pursuits/deal.md"
    _write_pursuit(pursuit, stage="pre-pipeline")

    resolved = resolve_pursuit_file(workspace, selector)

    assert resolved == pursuit


def test_pursuit_resolution_accepts_the_configured_workspace_alias(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    pursuit = workspace / "accounts/acme-corp/pursuits/deal.md"
    _write_pursuit(pursuit, stage="pre-pipeline")
    alias = tmp_path / "selected-workspace"
    alias.symlink_to(workspace, target_is_directory=True)

    resolved = resolve_pursuit_file(alias, str(alias / "accounts/acme-corp/pursuits/deal.md"))

    assert resolved == pursuit


@pytest.mark.parametrize("selector", ["acme-corp", "accounts/acme-corp/pursuits/template.md"])
def test_pursuit_resolution_rejects_obsolete_or_reserved_selectors(tmp_path: Path, selector: str) -> None:
    workspace = tmp_path / "workspace"
    _write_pursuit(workspace / "accounts/acme-corp/pursuits/template.md", stage="pre-pipeline")

    with pytest.raises(PursuitPathError, match="existing workspace file"):
        resolve_pursuit_file(workspace, selector)


def test_pursuit_resolution_fails_closed_when_workspace_is_missing(tmp_path: Path) -> None:
    with pytest.raises(PursuitPathError, match="existing workspace file"):
        resolve_pursuit_file(tmp_path / "missing", "acme-corp/deal")
