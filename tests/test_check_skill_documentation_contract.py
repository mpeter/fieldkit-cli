"""Contracts for deterministic semantic ownership of packaged skill documentation."""

import json
import os
from pathlib import Path

import pytest

from scripts import check_skill_documentation_contract as semantic_contract
from scripts.documentation_command_runner import CommandResult

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_brief_pages_have_one_fixed_semantic_suite() -> None:
    expected = {
        "src/fieldkit/skills/brief/SKILL.md",
        "src/fieldkit/skills/brief/ops/update.md",
        "src/fieldkit/skills/brief/ops/week-end.md",
        "src/fieldkit/skills/brief/ops/week-start.md",
        "src/fieldkit/skills/brief/references/comprehensive-scan.md",
    }
    suites = [suite for suite in semantic_contract._SUITES if expected.intersection(suite.paths)]

    assert len(suites) == 1
    assert set(suites[0].paths) == expected
    assert suites[0].argv == ("uv", "run", "pytest", "tests/test_brief_skill_documentation.py", "-q", "-n", "0")


@pytest.mark.parametrize(
    ("identifier", "paths", "test_file"),
    [
        (
            "meeting-reports",
            {
                "src/fieldkit/skills/meeting/ops/account-pulse.md",
                "src/fieldkit/skills/meeting/ops/stakeholder-map.md",
                "src/fieldkit/skills/meeting/ops/one-on-one.md",
            },
            "tests/test_meeting_workflow_scenarios.py",
        ),
        (
            "remaining-public-skills",
            {
                "src/fieldkit/skills/companion/SKILL.md",
                "src/fieldkit/skills/grill/SKILL.md",
                "src/fieldkit/skills/pursuit-advance/SKILL.md",
                "src/fieldkit/skills/pursuit-advance/gate-reference.md",
                "src/fieldkit/_data/pursuit-narrative-template.md",
            },
            "tests/test_remaining_public_skill_contracts.py",
        ),
        (
            "ingest",
            {
                "src/fieldkit/skills/ingest/SKILL.md",
                "src/fieldkit/skills/ingest/ops/gmail-refresh.md",
            },
            "tests/test_ingest_skill_documentation.py",
        ),
    ],
)
def test_completed_public_skill_pages_have_exactly_one_fixed_owner(
    identifier: str, paths: set[str], test_file: str
) -> None:
    suites = [suite for suite in semantic_contract._SUITES if paths.intersection(suite.paths)]

    assert len(suites) == 1
    assert suites[0].identifier == identifier
    assert set(suites[0].paths) == paths
    assert suites[0].argv == ("uv", "run", "pytest", test_file, "-q", "-n", "0")


def _write_contract(repo: Path, paths: list[str]) -> None:
    contract = {
        "verification": {
            "skill_semantic_contract": {
                "evidence": "uv run python -m scripts.check_skill_documentation_contract --json",
                "paths": paths,
            }
        }
    }
    path = repo / "docs/documentation-contract.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(contract), encoding="utf-8")


def test_check_rejects_contract_path_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A newly assigned or removed page needs an explicit semantic suite owner."""
    expected = sorted(path for suite in semantic_contract._SUITES for path in suite.paths)
    _write_contract(tmp_path, [*expected, "src/fieldkit/skills/unowned/SKILL.md"])
    monkeypatch.setattr(semantic_contract, "_validate_evidence_paths", lambda *_args: None)

    with pytest.raises(ValueError, match="path ownership mismatch"):
        semantic_contract.check(tmp_path)


def test_check_runs_each_fixed_suite_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each semantic suite executes one bounded argv and reports its exact pages."""
    expected = sorted(path for suite in semantic_contract._SUITES for path in suite.paths)
    _write_contract(tmp_path, expected)
    monkeypatch.setattr(semantic_contract, "_validate_evidence_paths", lambda *_args: None)
    observed: list[tuple[str, ...]] = []

    def run(repo_root: Path, argv: tuple[str, ...]) -> semantic_contract.SuiteResult:
        assert repo_root == tmp_path
        observed.append(argv)
        suite = next(suite for suite in semantic_contract._SUITES if suite.argv == argv)
        return semantic_contract.SuiteResult(suite.identifier, suite.paths, argv, 0, "passed", "")

    monkeypatch.setattr(semantic_contract, "_run", run)

    report = semantic_contract.check(tmp_path)

    assert report.status == "pass"
    assert observed == [suite.argv for suite in semantic_contract._SUITES]
    assert tuple(result.paths for result in report.results) == tuple(suite.paths for suite in semantic_contract._SUITES)


def test_check_preserves_a_failed_semantic_suite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One failed owner makes the document-level report fail."""
    expected = sorted(path for suite in semantic_contract._SUITES for path in suite.paths)
    _write_contract(tmp_path, expected)
    monkeypatch.setattr(semantic_contract, "_validate_evidence_paths", lambda *_args: None)

    def run(repo_root: Path, argv: tuple[str, ...]) -> semantic_contract.SuiteResult:
        suite = next(suite for suite in semantic_contract._SUITES if suite.argv == argv)
        exit_code = 1 if suite is semantic_contract._SUITES[0] else 0
        return semantic_contract.SuiteResult(suite.identifier, suite.paths, argv, exit_code, "", "failed")

    monkeypatch.setattr(semantic_contract, "_run", run)

    report = semantic_contract.check(tmp_path)

    assert report.status == "fail"
    assert report.results[0].exit_code == 1


def test_run_requires_every_owned_page_to_be_consumed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A green pytest exit cannot cover a suite page that no test opened."""
    suite = semantic_contract._SUITES[0]
    monkeypatch.setattr(semantic_contract, "_consumed_paths", lambda *_args: (suite.paths[0],))
    monkeypatch.setattr(semantic_contract, "_run_process", lambda *_args, **_kwargs: (0, "", ""))

    result = semantic_contract._run(tmp_path, suite.argv)

    assert result.exit_code == 1
    assert "unconsumed semantic documents" in result.stderr


def test_process_rejects_an_unregistered_inherited_command(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown semantic suite command"):
        semantic_contract._run_process(tmp_path, ("uv", "run", "pytest", "arbitrary.py"), {})


@pytest.mark.parametrize("inherited", [False, True])
def test_semantic_process_selects_only_verified_inherited_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inherited: bool
) -> None:
    argv = semantic_contract._SUITES[0].argv
    monkeypatch.setattr(semantic_contract, "_inherited_semantic_sandbox", lambda *_args: inherited)
    observed: list[object] = []

    def run(*_args: object, **kwargs: object) -> CommandResult:
        observed.append(kwargs["inherited_owner"])
        assert kwargs["timeout"] == semantic_contract._TIMEOUT_SECONDS
        return CommandResult(argv, 0, "passed", "")

    monkeypatch.setattr(semantic_contract, "_run_bounded", run)

    result = semantic_contract._run_process(tmp_path, argv, {})

    assert result == (0, "passed", "")
    assert observed == ["skill_semantic_contract" if inherited else None]


@pytest.mark.parametrize(
    "payload",
    [
        b'{"verification":{},"verification":{}}',
        b"[" * 65 + b"0" + b"]" * 65,
        b"[" * 2000 + b"0" + b"]" * 2000,
        b'{"private-example":"\xff"}',
    ],
    ids=["duplicate", "depth", "parser-recursion", "utf8"],
)
def test_contract_rejects_ambiguous_or_deep_control_without_reflection(
    tmp_path: Path, payload: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    contract = tmp_path / semantic_contract._CONTRACT_PATH
    contract.parent.mkdir()
    contract.write_bytes(payload)

    result = semantic_contract.main(["--repo-root", str(tmp_path)])

    assert result == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Skill documentation semantics: ERROR: invalid semantic documentation contract\n"


def test_contract_ownership_error_does_not_reflect_declared_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_contract(tmp_path, ["PRIVATE-CONTROL-SENTINEL" + "x" * 8192])

    result = semantic_contract.main(["--repo-root", str(tmp_path)])

    assert result == 2
    output = capsys.readouterr()
    assert "path ownership mismatch" in output.err
    assert "PRIVATE-CONTROL-SENTINEL" not in output.err
    assert len(output.err) < 200


@pytest.mark.parametrize("kind", ["contract", "coverage"])
def test_semantic_acquisition_rejects_oversize_before_decoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    path = tmp_path / (semantic_contract._CONTRACT_PATH if kind == "contract" else "coverage.txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 128)
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: pytest.fail("unbounded text read"))
    if kind == "contract":
        monkeypatch.setattr(semantic_contract, "_MAX_CONTRACT_BYTES", 64, raising=False)
        with pytest.raises(ValueError, match=r"^invalid semantic documentation contract$"):
            semantic_contract._contract_paths(tmp_path)
    else:
        monkeypatch.setattr(semantic_contract, "_MAX_COVERAGE_BYTES", 64, raising=False)
        with pytest.raises(ValueError, match=r"^invalid semantic document coverage$"):
            semantic_contract._consumed_paths(path, ("owned.md",))


@pytest.mark.parametrize("kind", ["contract", "coverage"])
@pytest.mark.parametrize("unsafe", ["symlink", "directory", "fifo", "utf8"])
def test_semantic_acquisition_rejects_unsafe_input(tmp_path: Path, kind: str, unsafe: str) -> None:
    path = tmp_path / (semantic_contract._CONTRACT_PATH if kind == "contract" else "coverage.txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    if unsafe == "symlink":
        target = tmp_path / "outside.txt"
        target.write_text("owned.md\n", encoding="utf-8")
        path.symlink_to(target)
    elif unsafe == "directory":
        path.mkdir()
    elif unsafe == "fifo":
        os.mkfifo(path)
    else:
        path.write_bytes(b"\xff")
    message = "invalid semantic documentation contract" if kind == "contract" else "invalid semantic document coverage"

    with pytest.raises(ValueError, match=f"^{message}$"):
        if kind == "contract":
            semantic_contract._contract_paths(tmp_path)
        else:
            semantic_contract._consumed_paths(path, ("owned.md",))


def test_coverage_preserves_missing_empty_and_repeated_owned_paths(tmp_path: Path) -> None:
    path = tmp_path / "coverage.txt"
    missing = semantic_contract._consumed_paths(path, ("owned.md",))
    assert missing == ()
    path.write_text("", encoding="utf-8")
    empty = semantic_contract._consumed_paths(path, ("owned.md",))
    assert empty == ()
    path.write_text("owned.md\nother.md\nowned.md\n", encoding="utf-8")
    observed = semantic_contract._consumed_paths(path, ("owned.md",))
    assert observed == ("owned.md",)


@pytest.mark.parametrize("depth", [64, 65])
def test_contract_accepts_exact_depth_boundary(tmp_path: Path, depth: int) -> None:
    nested: object = 0
    for _ in range(depth - 1):
        nested = [nested]
    payload = {"verification": {"skill_semantic_contract": {"paths": []}}, "extra": nested}
    contract = tmp_path / semantic_contract._CONTRACT_PATH
    contract.parent.mkdir()
    contract.write_text(json.dumps(payload), encoding="utf-8")

    if depth == 64:
        result = semantic_contract._contract_paths(tmp_path)
        assert result == ()
    else:
        with pytest.raises(ValueError, match=r"^invalid semantic documentation contract$"):
            semantic_contract._contract_paths(tmp_path)


@pytest.mark.parametrize("kind", ["contract", "coverage"])
@pytest.mark.parametrize("extra_byte", [False, True])
def test_semantic_acquisition_accepts_exact_byte_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, extra_byte: bool
) -> None:
    if kind == "contract":
        _write_contract(tmp_path, ["owned.md"])
        path = tmp_path / semantic_contract._CONTRACT_PATH
        bound_name = "_MAX_CONTRACT_BYTES"
    else:
        path = tmp_path / "coverage.txt"
        path.write_text("owned.md\n", encoding="utf-8")
        bound_name = "_MAX_COVERAGE_BYTES"
    monkeypatch.setattr(semantic_contract, bound_name, path.stat().st_size - int(extra_byte))

    if extra_byte:
        with pytest.raises(ValueError, match="invalid semantic"):
            if kind == "contract":
                semantic_contract._contract_paths(tmp_path)
            else:
                semantic_contract._consumed_paths(path, ("owned.md",))
    else:
        result = (
            semantic_contract._contract_paths(tmp_path)
            if kind == "contract"
            else semantic_contract._consumed_paths(path, ("owned.md",))
        )
        assert result == ("owned.md",)


@pytest.mark.parametrize("separator", ["\r", "\x00", "\u2028"])
def test_coverage_rejects_ambiguous_record_separators(tmp_path: Path, separator: str) -> None:
    path = tmp_path / "coverage.txt"
    path.write_text(f"unowned{separator}owned.md\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"^invalid semantic document coverage$"):
        semantic_contract._consumed_paths(path, ("owned.md",))
