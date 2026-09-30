"""Tests for the canonical public agent-instruction surface gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import check_agent_instruction_surface as surface

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_SCOPED_GUIDES = (
    "src/fieldkit/ingest/AGENTS.md",
    "src/fieldkit/llm/AGENTS.md",
    "src/fieldkit/pursuit/AGENTS.md",
    "src/fieldkit/sf/AGENTS.md",
    "src/fieldkit/watch/AGENTS.md",
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _fixture(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    policy = {
        "schema_version": 1,
        "expected_repository": "example/fieldkit-cli",
        "planned_tag": "v1.0.0",
        "rules": [
            {
                "id": "test",
                "action": "include",
                "category": "test",
                "patterns": [surface._PUBLIC_TEST, surface._QUALITY_TEST],
                "rationale": "Public regression coverage.",
            },
            {
                "id": "release",
                "action": "include",
                "category": "release_infrastructure",
                "patterns": [
                    surface._PUBLIC_GATE,
                    surface._QUALITY_RUNNER,
                    surface._QUALITY_SOURCE,
                    surface._QUALITY_PLAN,
                    surface._QUALITY_EXECUTION,
                    surface._QUALITY_RECEIPT,
                    surface._QUALITY_SCHEMA,
                ],
                "rationale": "Public verification infrastructure.",
            },
            {
                "id": "contribution",
                "action": "include",
                "category": "contribution",
                "patterns": ["AGENTS.md"],
                "rationale": "Canonical public contribution guidance.",
            },
            {
                "id": "product",
                "action": "include",
                "category": "product",
                "patterns": ["src/**"],
                "rationale": "Product source and scoped domain guidance.",
            },
            {
                "id": "build",
                "action": "include",
                "category": "build",
                "patterns": [".pre-commit-config.yaml"],
                "rationale": "Public contributor checks.",
            },
            {
                "id": "private-agent-state",
                "action": "exclude",
                "category": "local_tool_state",
                "patterns": ["CLAUDE.md", ".claude/**", ".opencode/**"],
                "rationale": "Private harness configuration.",
            },
        ],
    }
    _write(repo / surface._POLICY_PATH, json.dumps(policy))
    _write(repo / "AGENTS.md", "# Public contributor guidance\n")
    for path in _SCOPED_GUIDES:
        _write(repo / path, "# Scoped domain guidance\n")
    _write(repo / surface._PUBLIC_GATE, "# Public gate fixture.\n")
    (repo / surface._PUBLIC_GATE).chmod(0o755)
    _write(repo / surface._PUBLIC_TEST, "# Public test fixture.\n")
    _write(repo / surface._QUALITY_RUNNER, "# Fixed quality plan fixture.\n")
    _write(repo / surface._QUALITY_SOURCE, "# Source binding fixture.\n")
    _write(repo / surface._QUALITY_PLAN, "# Fixed plan fixture.\n")
    _write(repo / surface._QUALITY_EXECUTION, "# Bounded execution fixture.\n")
    _write(repo / surface._QUALITY_RECEIPT, "# Receipt validation fixture.\n")
    _write(repo / surface._QUALITY_TEST, "# Quality plan test fixture.\n")
    _write(repo / surface._QUALITY_SCHEMA, "{}\n")
    _write(
        repo / "Makefile",
        (f"quality:\n\tuv run python {surface._PUBLIC_GATE}\nquality-full:\n\tuv run python {surface._PUBLIC_GATE}\n"),
    )
    _write(repo / "hooks/post_commit.py", "# Package reinstall hook.\n")
    _write(repo / ".pre-commit-config.yaml", "# Public Markdown checks.\n")
    _write(repo / "tests/test_quality_contract.py", "# Public quality contract.\n")
    _write(repo / "openspec/specs/post-commit-worktree-install/spec.md", "# Package install provenance\n")
    _write(repo / "openspec/specs/markdown-link-integrity/spec.md", "# Public Markdown link coverage\n")
    return repo


def test_current_repository_has_one_root_and_reviewed_scoped_instruction_owners() -> None:
    assert surface.validate(_ROOT) == ()


def test_clean_public_export_without_private_adapters_passes(tmp_path: Path) -> None:
    repo = _fixture(tmp_path)

    result = surface.validate(repo)

    assert result == ()
    assert not (repo / ".claude").exists()
    assert not (repo / ".opencode").exists()


@pytest.mark.parametrize("path", _SCOPED_GUIDES)
def test_each_reviewed_scoped_guide_is_required(tmp_path: Path, path: str) -> None:
    repo = _fixture(tmp_path)
    (repo / path).unlink()

    assert surface.Finding("AIS008", path) in surface.validate(repo)


def test_unreviewed_exported_agent_guide_fails(tmp_path: Path) -> None:
    repo = _fixture(tmp_path)
    _write(repo / "src/fieldkit/new-domain/AGENTS.md", "# Undeclared guidance\n")

    assert surface.Finding("AIS009", "src/fieldkit/new-domain/AGENTS.md") in surface.validate(repo)


def test_excluded_private_adapter_sources_do_not_become_public_proof(tmp_path: Path) -> None:
    repo = _fixture(tmp_path)
    _write(repo / ".claude/commands/private.md", "private\n")
    _write(repo / ".opencode/commands/private.md", "private\n")

    assert surface.validate(repo) == ()


def test_public_gate_must_remain_executable(tmp_path: Path) -> None:
    repo = _fixture(tmp_path)
    (repo / surface._PUBLIC_GATE).chmod(0o644)

    assert surface.Finding("AIS008", surface._PUBLIC_GATE) in surface.validate(repo)


@pytest.mark.parametrize(
    ("path", "replacement", "reported_path"),
    [
        ("AGENTS.md", ".missing/AGENTS.md", "AGENTS.md"),
        ("CLAUDE.md", ".missing/CLAUDE.md", "CLAUDE.md"),
        (".claude/**", ".missing/.claude/**", ".claude/**"),
        (".opencode/**", ".missing/.opencode/**", ".opencode/**"),
        (surface._PUBLIC_GATE, ".missing/check.py", surface._PUBLIC_GATE),
        (surface._PUBLIC_TEST, ".missing/test.py", surface._PUBLIC_TEST),
        (surface._QUALITY_RUNNER, ".missing/quality.py", surface._QUALITY_RUNNER),
        (surface._QUALITY_TEST, ".missing/test-quality.py", surface._QUALITY_TEST),
        (surface._QUALITY_SCHEMA, ".missing/quality.schema.json", surface._QUALITY_SCHEMA),
    ],
)
def test_missing_or_unknown_policy_classification_fails(
    tmp_path: Path,
    path: str,
    replacement: str,
    reported_path: str,
) -> None:
    repo = _fixture(tmp_path)
    policy_path = repo / surface._POLICY_PATH
    policy_path.write_text(
        policy_path.read_text(encoding="utf-8").replace(f'"{path}"', f'"{replacement}"'), encoding="utf-8"
    )

    findings = surface.validate(repo)

    assert surface.Finding("AIS001", reported_path) in findings


@pytest.mark.parametrize("rule_id", ["contribution", "private-agent-state"])
def test_wrong_policy_action_or_category_fails(tmp_path: Path, rule_id: str) -> None:
    repo = _fixture(tmp_path)
    policy_path = repo / surface._POLICY_PATH
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    rule = next(candidate for candidate in policy["rules"] if candidate["id"] == rule_id)
    rule["action"] = "exclude" if rule_id == "contribution" else "include"
    rule["category"] = "local_tool_state" if rule_id == "contribution" else "build"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")

    findings = surface.validate(repo)

    assert any(finding.code == "AIS002" for finding in findings)


@pytest.mark.parametrize("path", surface._RETIRED_PATHS)
def test_retired_public_adapter_artifacts_fail(tmp_path: Path, path: str) -> None:
    repo = _fixture(tmp_path)
    _write(repo / path, "obsolete\n")

    assert surface.Finding("AIS003", path) in surface.validate(repo)


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("Makefile", "sync-claude:\n\tpython scripts/sync_claude_dir.py\n"),
        ("hooks/post_commit.py", 'if changed.startswith(".opencode/agents/"): pass\n'),
        ("openspec/specs/post-commit-worktree-install/spec.md", "Synchronize `.claude/` from private state.\n"),
        ("openspec/specs/markdown-link-integrity/spec.md", "Scan `.opencode/commands`.\n"),
    ],
)
def test_included_public_surface_rejects_retired_adapter_contract(tmp_path: Path, path: str, content: str) -> None:
    repo = _fixture(tmp_path)
    with (repo / path).open("a", encoding="utf-8") as handle:
        handle.write(content)

    findings = surface.validate(repo)

    assert any(finding.path == path and finding.code in {"AIS005", "AIS006"} for finding in findings)


@pytest.mark.parametrize("missing_tier", ["pr", "full"])
def test_both_quality_tiers_must_declare_the_non_skipping_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_tier: str
) -> None:
    repo = _fixture(tmp_path)
    original = surface._quality_plan_contains_gate
    monkeypatch.setattr(
        surface,
        "_quality_plan_contains_gate",
        lambda candidate_repo, tier: False if tier == missing_tier else original(candidate_repo, tier),
    )

    assert surface.Finding("AIS004", surface._QUALITY_RUNNER) in surface.validate(repo)


def test_make_overrides_are_not_misrepresented_as_execution_assurance(tmp_path: Path) -> None:
    repo = _fixture(tmp_path)
    _write(
        repo / "Makefile",
        "SHELL := /usr/bin/true\ninclude fictional-overrides.mk\n$(eval quality: ; @true)\n",
    )

    assert surface.validate(repo) == ()


@pytest.mark.parametrize("path", [".pre-commit-config.yaml", "tests/test_quality_contract.py"])
def test_public_quality_surfaces_reject_private_adapter_scope(tmp_path: Path, path: str) -> None:
    repo = _fixture(tmp_path)
    with (repo / path).open("a", encoding="utf-8") as stream:
        stream.write(".claude/agents .opencode/commands\n")

    assert surface.Finding("AIS006", path) in surface.validate(repo)


def test_main_does_not_reveal_private_policy_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    private_repo = tmp_path / "fictional-private-repository"
    private_repo.mkdir()

    exit_code = surface.main(["--repo", str(private_repo)])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert str(private_repo) not in captured.err
