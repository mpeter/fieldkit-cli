"""Adversarial contracts for the fixed-argv quality execution owner."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator

from scripts import quality_execution, quality_gate, quality_plan, quality_receipt, quality_source

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]


def test_cli_module_does_not_reexport_quality_authority() -> None:
    assert not hasattr(quality_gate, "build_plan")
    assert not hasattr(quality_gate, "execute_plan")
    assert not hasattr(quality_gate, "validate_receipt")


def _source_state(*, status_digest: str = "0" * 64, dirty_entries: int = 0) -> quality_source.SourceState:
    return quality_source.SourceState(
        head="a" * 40,
        head_tree="b" * 40,
        worktree="clean" if dirty_entries == 0 else "dirty",
        status_sha256=status_digest,
        index_sha256="2" * 64,
        worktree_sha256="1" * 64,
        dirty_entries=dirty_entries,
    )


def _test_plan(*commands: tuple[str, ...], timeout: float = 120.0) -> quality_plan.QualityPlan:
    return quality_plan.QualityPlan(
        tier="impact",
        stages=(
            quality_plan.StageSpec(
                label="fixed-test",
                commands=tuple(quality_plan.CommandSpec(argv=command) for command in commands),
            ),
        ),
        stage_timeout_seconds=timeout,
        quality_base="c" * 40,
        candidate_head="a" * 40,
        workers=4,
    )


def _execute(
    tmp_path: Path,
    plan: quality_plan.QualityPlan,
    monkeypatch: pytest.MonkeyPatch,
    *,
    states: tuple[quality_source.SourceState, ...] | None = None,
) -> dict[str, Any]:
    observed = iter(states or (_source_state(), _source_state()))
    monkeypatch.setattr(quality_execution, "capture_source_state", lambda _repo: next(observed))
    monkeypatch.setattr(
        quality_execution,
        "resolve_tool_identity",
        lambda argv, _environment: quality_execution.ToolIdentity(
            argv0=argv[0], resolved_name=Path(argv[0]).name, sha256="d" * 64
        ),
    )
    return quality_execution.execute_plan(plan, repo=tmp_path)


def _init_git_repo(repo: Path, *, content: str = "anchor\n") -> str:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=5)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=repo, check=True, timeout=5)
    subprocess.run(["git", "config", "user.name", "fieldkit test"], cwd=repo, check=True, timeout=5)
    (repo / "anchor.txt").write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "anchor.txt"], cwd=repo, check=True, timeout=5)
    subprocess.run(["git", "commit", "-qm", "test anchor"], cwd=repo, check=True, timeout=5)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()


@pytest.mark.parametrize("tier", ["pr", "full"])
def test_structured_document_gate_is_mandatory(tier: quality_plan.Tier) -> None:
    plan = quality_plan.build_plan(tier, repo=_ROOT, quality_base="c" * 40 if tier == "pr" else None)
    assert plan.tier == tier
    owners = [stage for stage in plan.stages if stage.label == "structured-document-contracts"]
    assert len(owners) == 1
    assert owners[0].skip_policy == "never"
    assert [command.argv for command in owners[0].commands] == [
        ("uv", "run", "python", "scripts/check_structured_document_contracts.py")
    ]


def test_canonical_plans_preserve_all_logical_stages_and_fixed_commands() -> None:
    pr = quality_plan.build_plan("pr", repo=_ROOT, quality_base="c" * 40, workers=4)
    full = quality_plan.build_plan("full", repo=_ROOT, workers=4)

    assert [stage.label for stage in pr.stages] == [
        "ruff-check",
        "ruff-format",
        "markdown-links",
        "docs-site",
        "agent-instruction-surface",
        "mypy",
        "tach",
        "dependency-profiles",
        "compatibility-policy",
        "public-identity",
        "structured-document-contracts",
        "public-tree-safety",
        "workflow-security",
        "release-workflow-policy",
        "supply-chain-policy",
        "release-policy",
        "quality-contract",
        "impact-pytest",
    ]
    assert [stage.label for stage in full.stages] == [
        "ruff-check",
        "ruff-format",
        "markdown-links",
        "docs-site",
        "mypy",
        "tach",
        "dependency-profiles",
        "compatibility-policy",
        "public-identity",
        "structured-document-contracts",
        "public-tree-safety",
        "workflow-security",
        "release-workflow-policy",
        "supply-chain-policy",
        "release-policy",
        "skill-integrity",
        "agent-instruction-surface",
        "click-params",
        "cli-docs",
        "dependency-map",
        "documentation-contract",
        "schema-sync",
        "changelog-fragment",
        "skillsaw",
        "pytest-coverage",
        "gazepy-baseline",
        "gazepy-ceiling",
        "gazepy-contract-coverage",
        "agentready-assess",
        "agentready-check",
        "behavioral-skill-eval",
        "flag-contract",
    ]
    assert len(pr.stages) == 18
    assert len(full.stages) == 32
    assert pr.stage_timeout_seconds == 120
    assert full.stage_timeout_seconds == 1800
    assert all(command.argv[0] != "make" for plan in (pr, full) for stage in plan.stages for command in stage.commands)

    docs = next(stage for stage in full.stages if stage.label == "docs-site")
    assert [command.argv for command in docs.commands] == [
        ("uv", "run", "python", "scripts/check_documentation_contract.py"),
        ("uv", "run", "python", "-m", "scripts.check_documentation_examples"),
        ("uv", "run", "mkdocs", "build", "--strict", "--site-dir", "build/site"),
        ("uv", "run", "python", "scripts/check_public_docs.py", "build/site"),
    ]
    assert next(stage for stage in full.stages if stage.label == "public-tree-safety").commands[0].argv == (
        "uv",
        "run",
        "python",
        "scripts/check_public_tree_safety.py",
        "--repo",
        ".",
    )
    hook_paths = tuple(path.relative_to(_ROOT).as_posix() for path in sorted((_ROOT / "hooks").glob("*.py")))
    mypy = next(stage for stage in full.stages if stage.label == "mypy").commands[0].argv
    assert mypy == ("uv", "run", "mypy", "src/fieldkit/", *hook_paths, "--no-error-summary")
    behavioral = next(stage for stage in full.stages if stage.label == "behavioral-skill-eval")
    assert behavioral.commands[0].environment == (("FIELDKIT_NO_LLM", "1"),)


def test_impact_plan_is_fixed_and_cannot_claim_a_pr_or_full_tier() -> None:
    plan = quality_plan.build_plan(
        "impact",
        repo=_ROOT,
        quality_base="c" * 40,
        candidate_head="a" * 40,
        workers=4,
        github_output="reports/github-output",
        selection_report="reports/test-selection.json",
        junitxml="reports/pytest.xml",
    )

    assert plan.tier == "impact"
    assert [stage.label for stage in plan.stages] == ["impact-pytest"]
    assert plan.stages[0].commands[0].argv == (
        "uv",
        "run",
        "python",
        "scripts/run_impact_tests.py",
        "--base",
        "c" * 40,
        "--head",
        "a" * 40,
        "--github-output",
        "reports/github-output",
        "--selection-report",
        "reports/test-selection.json",
        "--junitxml=reports/pytest.xml",
    )
    with pytest.raises(ValueError, match="repository-relative"):
        quality_plan.build_plan(
            "impact",
            repo=_ROOT,
            quality_base="c" * 40,
            candidate_head="a" * 40,
            workers=4,
            junitxml="/private/pytest.xml",
        )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SHELL", "/usr/bin/true"),
        ("MAKEFLAGS", "--eval=quality:;@true"),
        ("MAKEFILES", "override.mk"),
    ],
)
def test_make_environment_cannot_change_direct_fixed_argv_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    (tmp_path / "Makefile").write_text("quality:\n\t@false\n", encoding="utf-8")
    (tmp_path / "GNUmakefile").write_text("quality:\n\t@true\n", encoding="utf-8")
    (tmp_path / "override.mk").write_text("$(eval quality: ; @true)\n", encoding="utf-8")
    monkeypatch.setenv(name, value)
    plan = _test_plan((sys.executable, "-I", "-c", "print('fixed child')"))

    receipt = _execute(tmp_path, plan, monkeypatch)

    command = receipt["stages"][0]["commands"][0]
    assert receipt["status"] == "pass"
    assert command["argv"] == [sys.executable, "-I", "-c", "print('fixed child')"]
    assert command["stdout"]["bytes"] == len(b"fixed child\n")


@pytest.mark.parametrize(
    ("command", "timeout", "reason"),
    [
        (("/definitely/missing/fieldkit-quality-child",), 5.0, "start"),
        ((sys.executable, "-I", "-c", "import time; time.sleep(30)"), 0.05, "timeout"),
        ((sys.executable, "-I", "-c", "import os; os.write(1, b'x' * 9000000)"), 5.0, "overflow"),
    ],
)
def test_start_timeout_and_overflow_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: tuple[str, ...],
    timeout: float,
    reason: str,
) -> None:
    receipt = _execute(tmp_path, _test_plan(command, timeout=timeout), monkeypatch)

    command_record = receipt["stages"][0]["commands"][0]
    assert receipt["status"] == "fail"
    assert receipt["complete"] is False
    assert command_record["status"] == "fail"
    assert command_record["failure_reason"] == reason
    assert command_record["stdout"] == {"status": "unavailable", "bytes": None, "sha256": None}
    assert command_record["stderr"] == {"status": "unavailable", "bytes": None, "sha256": None}


def test_timeout_kills_a_descendant_holding_the_output_pipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "late-marker"
    child = f"import time,pathlib; time.sleep(.3); pathlib.Path({str(marker)!r}).write_text('late')"
    parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-I','-c',{child!r}]); time.sleep(30)"

    receipt = _execute(
        tmp_path,
        _test_plan((sys.executable, "-I", "-c", parent), timeout=0.05),
        monkeypatch,
    )
    time.sleep(0.4)

    assert receipt["status"] == "fail"
    assert receipt["stages"][0]["commands"][0]["failure_reason"] == "timeout"
    assert not marker.exists()


def test_child_json_cannot_forge_aggregate_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"schema_version": 1, "status": "pass", "complete": True})
    plan = _test_plan((sys.executable, "-I", "-c", f"print({payload!r}); raise SystemExit(9)"))

    receipt = _execute(tmp_path, plan, monkeypatch)

    record = receipt["stages"][0]["commands"][0]
    assert receipt["status"] == "fail"
    assert record["exit_status"] == 9
    assert "schema_version" not in record
    assert set(record["stdout"]) == {"status", "bytes", "sha256"}
    assert record["stdout"]["status"] == "complete"


def test_real_execution_receipt_validates_before_adversarial_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _test_plan((sys.executable, "-I", "-c", "print('complete')"))

    receipt = _execute(tmp_path, plan, monkeypatch)

    assert quality_receipt.validate_receipt(receipt, plan=plan) == ()
    assert quality_receipt.receipt_passes(receipt, plan=plan) is True


def test_stage_execution_scrubs_ambient_git_repository_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_DIR", "/private/decoy.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/private/decoy")
    plan = _test_plan(
        (
            sys.executable,
            "-I",
            "-c",
            "import os; raise SystemExit('GIT_DIR' in os.environ or 'GIT_WORK_TREE' in os.environ)",
        )
    )

    receipt = _execute(tmp_path, plan, monkeypatch)

    assert receipt["status"] == "pass"


def test_runner_digest_binds_all_controller_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    primary = tmp_path / "primary.py"
    helper = tmp_path / "helper.py"
    primary.write_text("primary\n", encoding="utf-8")
    helper.write_text("helper1\n", encoding="utf-8")
    monkeypatch.setattr(quality_receipt, "_ROOT", tmp_path)
    monkeypatch.setattr(quality_receipt, "_RUNNER_SOURCE_PATHS", (Path("primary.py"), Path("helper.py")))
    before = quality_receipt.runner_sha256()

    helper.write_text("helper2\n", encoding="utf-8")

    assert quality_receipt.runner_sha256() != before


def test_only_pr_impact_may_use_the_prose_skip() -> None:
    pr = quality_plan.build_plan("pr", repo=_ROOT, quality_base="c" * 40, workers=4)
    full = quality_plan.build_plan("full", repo=_ROOT, workers=4)

    assert [stage.skip_policy for stage in pr.stages].count("prose-only") == 1
    assert next(stage for stage in pr.stages if stage.skip_policy == "prose-only").label == "impact-pytest"
    assert all(stage.skip_policy == "never" for stage in full.stages)


def test_source_drift_forces_a_nonpassing_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = _source_state()
    after = _source_state(status_digest="e" * 64, dirty_entries=1)

    receipt = _execute(
        tmp_path,
        _test_plan((sys.executable, "-I", "-c", "pass")),
        monkeypatch,
        states=(before, after),
    )

    assert receipt["status"] == "fail"
    assert receipt["failure_reason"] == "source-drift"
    assert receipt["source_before"] != receipt["source_after"]


@pytest.mark.parametrize("tracked", [True, False])
def test_source_binding_detects_same_status_content_mutation(tmp_path: Path, tracked: bool) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=5)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=tmp_path, check=True, timeout=5)
    subprocess.run(["git", "config", "user.name", "fieldkit test"], cwd=tmp_path, check=True, timeout=5)
    anchor = tmp_path / "anchor.txt"
    anchor.write_text("anchor\n", encoding="utf-8")
    subprocess.run(["git", "add", "anchor.txt"], cwd=tmp_path, check=True, timeout=5)
    subprocess.run(["git", "commit", "-qm", "test anchor"], cwd=tmp_path, check=True, timeout=5)
    candidate = tmp_path / "candidate.txt"
    if tracked:
        candidate.write_text("original\n", encoding="utf-8")
        subprocess.run(["git", "add", "candidate.txt"], cwd=tmp_path, check=True, timeout=5)
        subprocess.run(["git", "commit", "-qm", "test candidate"], cwd=tmp_path, check=True, timeout=5)
    candidate.write_text("first---\n", encoding="utf-8")
    before = quality_source.capture_source_state(tmp_path)

    candidate.write_text("second--\n", encoding="utf-8")
    after = quality_source.capture_source_state(tmp_path)

    assert before.status_sha256 == after.status_sha256
    assert before.worktree_sha256 != after.worktree_sha256


def test_source_binding_ignores_ambient_git_repository_redirection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real"
    decoy = tmp_path / "decoy"
    real_head = _init_git_repo(real, content="real\n")
    decoy_head = _init_git_repo(decoy, content="decoy\n")
    assert real_head != decoy_head
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))

    state = quality_source.capture_source_state(real)

    assert state.head == real_head


@pytest.mark.parametrize("entry_kind", ["mode", "symlink"])
def test_source_binding_covers_modes_and_symlink_targets(tmp_path: Path, entry_kind: str) -> None:
    _init_git_repo(tmp_path)
    candidate = tmp_path / "candidate"
    if entry_kind == "mode":
        candidate.write_text("content\n", encoding="utf-8")
    else:
        candidate.symlink_to("original-target")
    subprocess.run(["git", "add", "candidate"], cwd=tmp_path, check=True, timeout=5)
    subprocess.run(["git", "commit", "-qm", "test candidate"], cwd=tmp_path, check=True, timeout=5)
    if entry_kind == "mode":
        candidate.chmod(0o755)
    else:
        candidate.unlink()
        candidate.symlink_to("first---target")
    before = quality_source.capture_source_state(tmp_path)

    if entry_kind == "mode":
        candidate.chmod(0o700)
    else:
        candidate.unlink()
        candidate.symlink_to("second--target")
    after = quality_source.capture_source_state(tmp_path)

    assert before.status_sha256 == after.status_sha256
    assert before.worktree_sha256 != after.worktree_sha256


def test_source_binding_records_deleted_parent_without_following_replacement_symlink(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    nested = tmp_path / "tracked" / "child.txt"
    nested.parent.mkdir()
    nested.write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked/child.txt"], cwd=tmp_path, check=True, timeout=5)
    subprocess.run(["git", "commit", "-qm", "test nested file"], cwd=tmp_path, check=True, timeout=5)

    nested.unlink()
    nested.parent.rmdir()
    missing = quality_source.capture_source_state(tmp_path)
    assert missing.worktree == "dirty"
    assert quality_source.capture_source_state(tmp_path) == missing

    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "tracked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe ancestor"):
        quality_source.capture_source_state(tmp_path)


def test_source_binding_detects_same_status_index_mutation(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=5)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=tmp_path, check=True, timeout=5)
    subprocess.run(["git", "config", "user.name", "fieldkit test"], cwd=tmp_path, check=True, timeout=5)
    candidate = tmp_path / "candidate.txt"
    candidate.write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "add", "candidate.txt"], cwd=tmp_path, check=True, timeout=5)
    subprocess.run(["git", "commit", "-qm", "test candidate"], cwd=tmp_path, check=True, timeout=5)
    candidate.write_text("working-\n", encoding="utf-8")

    def stage_blob(content: bytes) -> None:
        result = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=tmp_path,
            input=content,
            capture_output=True,
            check=True,
            timeout=5,
        )
        object_id = result.stdout.decode("ascii").strip()
        subprocess.run(
            ["git", "update-index", "--cacheinfo", f"100644,{object_id},candidate.txt"],
            cwd=tmp_path,
            check=True,
            timeout=5,
        )

    stage_blob(b"staged-a\n")
    before = quality_source.capture_source_state(tmp_path)
    stage_blob(b"staged-b\n")
    after = quality_source.capture_source_state(tmp_path)

    assert before.status_sha256 == after.status_sha256
    assert before.worktree_sha256 == after.worktree_sha256
    assert before.index_sha256 != after.index_sha256


def test_same_status_mutation_during_execution_forces_source_drift(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=5)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=tmp_path, check=True, timeout=5)
    subprocess.run(["git", "config", "user.name", "fieldkit test"], cwd=tmp_path, check=True, timeout=5)
    candidate = tmp_path / "candidate.txt"
    candidate.write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "add", "candidate.txt"], cwd=tmp_path, check=True, timeout=5)
    subprocess.run(["git", "commit", "-qm", "test candidate"], cwd=tmp_path, check=True, timeout=5)
    candidate.write_text("first---\n", encoding="utf-8")
    plan = _test_plan(
        (
            sys.executable,
            "-I",
            "-c",
            "from pathlib import Path; Path('candidate.txt').write_text('second--\\n', encoding='utf-8')",
        )
    )

    receipt = quality_execution.execute_plan(plan, repo=tmp_path)
    source_before = cast(dict[str, Any], receipt["source_before"])
    source_after = cast(dict[str, Any], receipt["source_after"])

    assert receipt["status"] == "fail"
    assert receipt["failure_reason"] == "source-drift"
    assert source_before["status_sha256"] == source_after["status_sha256"]
    assert source_before["worktree_sha256"] != source_after["worktree_sha256"]


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "duplicate",
        "reordered",
        "plan-digest",
        "runner-digest",
        "stage-status",
        "source-drift",
        "candidate-scope",
        "failure-reason",
        "command-timeout",
        "tool-identity",
        "command-pass-state",
        "generated-at",
    ],
)
def test_receipt_verifier_rejects_incomplete_or_mixed_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    plan = _test_plan(
        (sys.executable, "-I", "-c", "print('one')"),
        (sys.executable, "-I", "-c", "print('two')"),
    )
    receipt = _execute(tmp_path, plan, monkeypatch)
    assert quality_receipt.validate_receipt(receipt, plan=plan) == ()
    assert quality_receipt.receipt_passes(receipt, plan=plan) is True
    broken = copy.deepcopy(receipt)
    commands = broken["stages"][0]["commands"]
    if fault == "missing":
        commands.pop()
    elif fault == "duplicate":
        commands.append(copy.deepcopy(commands[0]))
    elif fault == "reordered":
        commands.reverse()
    elif fault == "plan-digest":
        broken["plan"]["sha256"] = "f" * 64
    elif fault == "runner-digest":
        broken["plan"]["runner_sha256"] = "f" * 64
    elif fault == "stage-status":
        broken["stages"][0]["commands"][0]["status"] = "fail"
        broken["stages"][0]["commands"][0]["failure_reason"] = "nonzero-exit"
        broken["stages"][0]["commands"][0]["exit_status"] = 1
    elif fault == "source-drift":
        broken["source_after"]["status_sha256"] = "f" * 64
    elif fault == "candidate-scope":
        broken["candidate_scope"] = "dirty-local"
    elif fault == "failure-reason":
        broken["failure_reason"] = "stage-failed"
    elif fault == "command-timeout":
        commands[0]["timeout_seconds"] = 1
    elif fault == "tool-identity":
        commands[0]["tool"]["argv0"] = "different-tool"
    elif fault == "command-pass-state":
        commands[0]["exit_status"] = 9
    else:
        broken["generated_at"] = "not-a-date"

    findings = quality_receipt.validate_receipt(broken, plan=plan)

    assert findings
    assert quality_receipt.receipt_passes(broken, plan=plan) is False


def test_receipt_is_strictly_schema_valid_without_private_environment_or_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FICTIONAL_SECRET", "do-not-record")
    receipt = _execute(
        tmp_path,
        _test_plan((sys.executable, "-I", "-c", "import os; print(os.environ['FICTIONAL_SECRET'])")),
        monkeypatch,
    )
    schema = json.loads((_ROOT / "docs/release-readiness/quality-gate-receipt.schema.json").read_text(encoding="utf-8"))
    serialized = json.dumps(receipt, sort_keys=True)

    assert list(Draft202012Validator(schema).iter_errors(receipt)) == []
    assert "do-not-record" not in serialized
    assert receipt["stages"][0]["commands"][0]["environment"] == {}
    assert receipt["trust"] == "unqualified-controller"
    assert receipt["immutable_release_evidence"] is False
    assert receipt["unproven_gates"] == [
        "external-controller-qualification",
        "maintained-runtime-qualification",
        "filesystem-isolation",
        "network-isolation",
        "pinned-toolchain-qualification",
        "immutable-same-candidate-release-evidence",
    ]


def test_dirty_local_success_is_never_immutable_release_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dirty = _source_state(status_digest="e" * 64, dirty_entries=2)

    receipt = _execute(
        tmp_path,
        _test_plan((sys.executable, "-I", "-c", "pass")),
        monkeypatch,
        states=(dirty, dirty),
    )

    assert receipt["status"] == "pass"
    assert receipt["candidate_scope"] == "dirty-local"
    assert receipt["immutable_release_evidence"] is False


def test_schema_rejects_unknown_fields_and_unbounded_values() -> None:
    schema = json.loads((_ROOT / "docs/release-readiness/quality-gate-receipt.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    assert list(validator.iter_errors({"schema_version": 1, "unexpected": True}))


@pytest.mark.parametrize(
    "content",
    [b'{"schema_version":1,"schema_version":1}', b"x" * (quality_receipt.RECEIPT_LIMIT_BYTES + 1)],
    ids=["duplicate-keys", "oversized-input"],
)
def test_receipt_reader_rejects_duplicate_keys_and_oversized_input(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "receipt.json"
    path.write_bytes(content)

    with pytest.raises(ValueError, match=r"duplicate|byte bound"):
        quality_receipt.read_bounded_json(path, quality_receipt.RECEIPT_LIMIT_BYTES)


def test_missing_controller_receipt_is_nonpassing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(_ROOT)
    missing = "reports/fictional-missing-quality-receipt.json"

    result = quality_gate.main(["verify", "--tier", "full", "--receipt", missing])

    captured = capsys.readouterr()
    assert result == 2
    assert "controller could not produce valid evidence" in captured.err
    assert str(_ROOT) not in captured.err
