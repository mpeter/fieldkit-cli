"""Unit tests for scripts/check_changelog_fragment.py and scripts/build_changelog.py.

Covers the doc-only exemption, fragment recognition, the skip-changelog waiver,
the failing path that blocks a PR, and fragment assembly into CHANGELOG.md.
"""

import importlib.util
import re
import types
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Load the scripts as modules (they live in scripts/, not a package).
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


def _load_module(name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_check = _load_module("check_changelog_fragment")
_build = _load_module("build_changelog")
_PRIVATE_TRACKER_ID = "bug" + "-890"


# ---------------------------------------------------------------------------
# _is_doc_only — must agree with the `changes` job globs in ci.yml
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        "docs/ops-runbook.md",
        "docs/subtleties/sf.md",
        "openspec/changes/foo/tasks.md",
        ".opencode/skills/proctor/SKILL.md",
        ".specify/memory.md",
        "AGENTS.md",
        "CHANGELOG.md",
        "changelog.d/fix-bare-pytest-raises.md",
    ],
)
def test_is_doc_only_accepts_doc_paths(path: str) -> None:
    """Doc paths and any .md file are exempt from the fragment requirement."""
    result = _check._is_doc_only(path)
    assert result is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        "src/fieldkit/sf/client.py",
        "hooks/pii_guard.py",
        "Makefile",
        "pyproject.toml",
        ".github/workflows/ci.yml",
    ],
)
def test_is_doc_only_rejects_code_paths(path: str) -> None:
    """Code, build, and CI files require a fragment."""
    result = _check._is_doc_only(path)
    assert result is False


# ---------------------------------------------------------------------------
# _is_fragment
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_is_fragment_accepts_slug_named_file() -> None:
    """A slug-named .md directly under changelog.d/ is a real fragment."""
    result = _check._is_fragment("changelog.d/fix-bare-pytest-raises.md")
    assert result is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        "changelog.d/README.md",  # convention doc, not a change
        "changelog.d/.gitkeep",  # placeholder
        "changelog.d/nested/entry.md",  # not directly under changelog.d/
        "docs/fix-bare-pytest-raises.md",  # right name, wrong directory
        "changelog.d/entry.txt",  # not markdown
    ],
)
def test_is_fragment_rejects_non_fragments(path: str) -> None:
    """Meta files, nested paths, and non-markdown do not satisfy the gate."""
    result = _check._is_fragment(path)
    assert result is False


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "content", "expected_problem"),
    [
        (
            f"changelog.d/{_PRIVATE_TRACKER_ID}-missing-config.md",
            "### Fix the missing configuration message\n",
            "private tracker identifier",
        ),
        (
            "changelog.d/fix-missing-config.md",
            f"### Fix {_PRIVATE_TRACKER_ID} configuration handling\n",
            "private tracker identifier",
        ),
        (
            "changelog.d/Fix missing config.md",
            "### Fix missing configuration handling\n",
            "lowercase descriptive slug",
        ),
        (
            "changelog.d/fix-missing-config.md",
            "Configuration handling is now actionable.\n",
            "level-three heading",
        ),
        (
            "changelog.d/fix-missing-config.md",
            "### Fix missing configuration handling\n",
            "observable result",
        ),
        (
            "changelog.d/fix-missing-config.md",
            "### \n\nThe error is actionable.\n",
            "descriptive heading",
        ),
    ],
)
def test_fragment_problem_rejects_private_or_malformed_public_entries(
    path: str, content: str, expected_problem: str
) -> None:
    """Public fragments cannot depend on private trackers or ambiguous formatting."""
    result = _check._fragment_problem(path, content)

    assert result is not None
    assert expected_problem in result


@pytest.mark.unit
def test_fragment_problem_accepts_descriptive_slug_and_public_issue() -> None:
    """A descriptive fragment may cite a public GitHub issue."""
    result = _check._fragment_problem(
        "changelog.d/fix-missing-config.md",
        "### Fix the missing configuration message (#890)\n\nThe error is actionable.\n",
    )

    assert result is None


# ---------------------------------------------------------------------------
# main() — the gate itself
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_main_waives_on_skip_changelog(monkeypatch: pytest.MonkeyPatch) -> None:
    """SKIP_CHANGELOG=true short-circuits before any git call."""
    monkeypatch.setenv("SKIP_CHANGELOG", "true")
    with patch.object(_check, "_git") as mock_git:
        exit_code = _check.main()
    assert exit_code == 0
    mock_git.assert_not_called()


@pytest.mark.unit
def test_main_passes_when_only_docs_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A doc-only change needs no fragment."""
    monkeypatch.delenv("SKIP_CHANGELOG", raising=False)

    def fake_git(*args: str) -> str:
        if args[0] == "merge-base":
            return "abc123"
        if args[0] == "diff":
            return "docs/ops-runbook.md\nAGENTS.md"
        return ""

    with patch.object(_check, "_git", side_effect=fake_git):
        exit_code = _check.main()
    assert exit_code == 0


@pytest.mark.unit
def test_main_passes_when_fragment_added(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Code change plus a fragment satisfies the gate."""
    fragment = tmp_path / "changelog.d" / "fix-quota-output.md"
    fragment.parent.mkdir()
    fragment.write_text("### Fix quota output\n\nThe total is now correct.\n", encoding="utf-8")
    monkeypatch.setattr(_check, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("SKIP_CHANGELOG", raising=False)

    def fake_git(*args: str) -> str:
        if args[0] == "merge-base":
            return "abc123"
        if args[0] == "diff" and "--diff-filter=A" in args:
            return "changelog.d/fix-quota-output.md"
        if args[0] == "diff":
            return "src/fieldkit/sf/client.py\nchangelog.d/fix-quota-output.md"
        return ""

    with patch.object(_check, "_git", side_effect=fake_git):
        exit_code = _check.main()
    assert exit_code == 0


@pytest.mark.unit
def test_main_rejects_private_tracker_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file in changelog.d cannot satisfy the gate when it exposes a private ID."""
    fragment = tmp_path / "changelog.d" / f"{_PRIVATE_TRACKER_ID}-missing-config.md"
    fragment.parent.mkdir()
    fragment.write_text("### Fix the missing configuration message\n", encoding="utf-8")
    monkeypatch.setattr(_check, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("SKIP_CHANGELOG", raising=False)

    def fake_git(*args: str) -> str:
        if args[0] == "merge-base":
            return "abc123"
        if args[0] == "diff" and "--diff-filter=A" in args:
            return f"changelog.d/{_PRIVATE_TRACKER_ID}-missing-config.md"
        if args[0] == "diff":
            return f"src/fieldkit/config.py\nchangelog.d/{_PRIVATE_TRACKER_ID}-missing-config.md"
        return ""

    with patch.object(_check, "_git", side_effect=fake_git):
        exit_code = _check.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "private tracker identifier" in captured.err


@pytest.mark.unit
def test_main_fails_on_code_change_without_fragment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The conflict-causing case: code changed, no fragment, no waiver."""
    monkeypatch.delenv("SKIP_CHANGELOG", raising=False)

    def fake_git(*args: str) -> str:
        if args[0] == "merge-base":
            return "abc123"
        if args[0] == "diff":
            return "src/fieldkit/sf/client.py"
        return ""

    with patch.object(_check, "_git", side_effect=fake_git):
        exit_code = _check.main()
    assert exit_code == 1


@pytest.mark.unit
def test_main_counts_untracked_code_as_a_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """A brand-new uncommitted .py file still demands a fragment.

    `git diff` omits untracked files, so without folding in `ls-files --others`
    a branch adding only new modules passes locally and fails in CI once
    committed — the verdict must not depend on whether `git commit` has run.
    """
    monkeypatch.delenv("SKIP_CHANGELOG", raising=False)

    def fake_git(*args: str) -> str:
        if args[0] == "merge-base":
            return "abc123"
        if args[0] == "diff":
            return ""
        if args[0] == "ls-files":
            return "src/fieldkit/watch/brand_new.py"
        return ""

    with patch.object(_check, "_git", side_effect=fake_git):
        exit_code = _check.main()
    assert exit_code == 1


@pytest.mark.unit
def test_main_fails_when_changelog_edited_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Editing CHANGELOG.md does NOT substitute for a fragment on a code change."""
    monkeypatch.delenv("SKIP_CHANGELOG", raising=False)

    def fake_git(*args: str) -> str:
        if args[0] == "merge-base":
            return "abc123"
        if args[0] == "diff":
            return "src/fieldkit/sf/client.py\nCHANGELOG.md"
        return ""

    with patch.object(_check, "_git", side_effect=fake_git):
        exit_code = _check.main()
    assert exit_code == 1


# ---------------------------------------------------------------------------
# build_changelog._assemble
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_assemble_inserts_fragments_under_unreleased(tmp_path: Path) -> None:
    """Fragments land directly under the Unreleased heading, above prior entries."""
    fragment = tmp_path / "fix-quota-period.md"
    fragment.write_text("### Fix quota period end date\n\nBody.\n", encoding="utf-8")
    changelog = "# Changelog\n\n## [Unreleased]\n\n### Older entry\n\nPrior body.\n"

    assembled = _build._assemble(changelog, [fragment])

    assert "### Fix quota period end date" in assembled
    assert assembled.index("Fix quota period end date") < assembled.index("Older entry")
    assert "Prior body." in assembled


@pytest.mark.unit
def test_assemble_removes_empty_unreleased_placeholder(tmp_path: Path) -> None:
    """The empty-state sentence must not survive after a fragment is assembled."""
    fragment = tmp_path / "fix-actionable-error.md"
    fragment.write_text(
        "### Make configuration errors actionable\n\nThe message now names the active file.\n", encoding="utf-8"
    )
    changelog = "# Changelog\n\n## [Unreleased]\n\nNo public changes yet.\n\n## [1.0.0]\n"

    assembled = _build._assemble(changelog, [fragment])

    assert "Make configuration errors actionable" in assembled
    assert "No public changes yet." not in assembled
    assert "## [1.0.0]" in assembled


@pytest.mark.unit
def test_assemble_separates_entries_with_rule(tmp_path: Path) -> None:
    """Assembled entries are separated by the same `---` rule used by hand."""
    first = tmp_path / "a-one.md"
    first.write_text("### One\n", encoding="utf-8")
    second = tmp_path / "b-two.md"
    second.write_text("### Two\n", encoding="utf-8")

    assembled = _build._assemble("# Changelog\n\n## [Unreleased]\n", [first, second])

    assert "\n\n---\n\n" in assembled
    assert assembled.rstrip().endswith("### Two")


@pytest.mark.unit
def test_assemble_raises_without_unreleased_anchor(tmp_path: Path) -> None:
    """A CHANGELOG missing the anchor is a hard error, not a silent no-op."""
    fragment = tmp_path / "fix-missing-config.md"
    fragment.write_text("### Bug\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"\[Unreleased\]"):
        _build._assemble("# Changelog\n\n## [1.0.0]\n", [fragment])


@pytest.mark.unit
def test_collect_fragments_excludes_readme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """README.md documents the convention and is never consumed as an entry."""
    monkeypatch.setattr(_build, "FRAGMENTS_DIR", tmp_path)
    (tmp_path / "README.md").write_text("docs", encoding="utf-8")
    (tmp_path / "fix-missing-config.md").write_text("### Fix", encoding="utf-8")

    fragments = _build._collect_fragments()

    assert [p.name for p in fragments] == ["fix-missing-config.md"]


@pytest.mark.unit
def test_assemble_skips_heading_already_in_changelog(tmp_path: Path) -> None:
    """A fragment whose heading is already present is not folded in twice.

    Guards the interrupted-cleanup path: CHANGELOG.md written, fragments only
    partly deleted, operator re-runs `make changelog`.
    """
    fragment = tmp_path / "fix-missing-config.md"
    fragment.write_text("### Fix missing configuration — already landed\n\nBody.\n", encoding="utf-8")
    changelog = "# Changelog\n\n## [Unreleased]\n\n### Fix missing configuration — already landed\n\nBody.\n"

    assembled = _build._assemble(changelog, [fragment])

    assert assembled.count("### Fix missing configuration — already landed") == 1


# ---------------------------------------------------------------------------
# build_changelog.main() — the destructive path
# ---------------------------------------------------------------------------


def _stage_changelog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Point the builder at a throwaway CHANGELOG.md and fragments dir."""
    fragments_dir = tmp_path / "changelog.d"
    fragments_dir.mkdir()
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## [Unreleased]\n\n### Older\n", encoding="utf-8")
    monkeypatch.setattr(_build, "FRAGMENTS_DIR", fragments_dir)
    monkeypatch.setattr(_build, "CHANGELOG_PATH", changelog)
    return changelog, fragments_dir


@pytest.mark.unit
def test_main_dry_run_mutates_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--dry-run must leave both CHANGELOG.md and the fragments untouched."""
    changelog, fragments_dir = _stage_changelog(tmp_path, monkeypatch)
    fragment = fragments_dir / "fix-missing-config.md"
    fragment.write_text("### Fix missing configuration\n", encoding="utf-8")
    before = changelog.read_text(encoding="utf-8")

    exit_code = _build.main(["--dry-run"])

    assert exit_code == 0
    assert changelog.read_text(encoding="utf-8") == before
    assert fragment.exists()


@pytest.mark.unit
def test_main_assembles_and_deletes_fragments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The default path folds fragments in and removes them."""
    changelog, fragments_dir = _stage_changelog(tmp_path, monkeypatch)
    fragment = fragments_dir / "fix-missing-config.md"
    fragment.write_text("### Fix missing configuration — new entry\n", encoding="utf-8")

    exit_code = _build.main([])

    assert exit_code == 0
    assert "### Fix missing configuration — new entry" in changelog.read_text(encoding="utf-8")
    assert not fragment.exists()


@pytest.mark.unit
def test_main_rejects_unknown_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `--dryrun` typo must not silently run the destructive path."""
    changelog, fragments_dir = _stage_changelog(tmp_path, monkeypatch)
    fragment = fragments_dir / "fix-missing-config.md"
    fragment.write_text("### Fix missing configuration\n", encoding="utf-8")
    before = changelog.read_text(encoding="utf-8")

    with pytest.raises(SystemExit, match="2"):
        _build.main(["--dryrun"])

    assert changelog.read_text(encoding="utf-8") == before
    assert fragment.exists()


@pytest.mark.unit
def test_main_rerun_after_partial_cleanup_does_not_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fragment surviving a failed unlink is not folded in a second time."""
    changelog, fragments_dir = _stage_changelog(tmp_path, monkeypatch)
    fragment = fragments_dir / "fix-missing-config.md"
    fragment.write_text("### Fix missing configuration — survivor\n", encoding="utf-8")

    # First run: assembly succeeds, cleanup fails, so the fragment stays on disk.
    def _all_survive(frags: list[Path]) -> list[Path]:
        return list(frags)

    monkeypatch.setattr(_build, "_delete_fragments", _all_survive)
    first_exit = _build.main([])

    assert first_exit == 1
    assert fragment.exists()

    # Second run over the same surviving fragment must not duplicate the entry.
    monkeypatch.undo()
    monkeypatch.setattr(_build, "FRAGMENTS_DIR", fragments_dir)
    monkeypatch.setattr(_build, "CHANGELOG_PATH", changelog)
    _build.main([])

    assert changelog.read_text(encoding="utf-8").count("### Fix missing configuration — survivor") == 1


# ---------------------------------------------------------------------------
# Cross-file invariant: one definition of "doc-only" for the repo
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_doc_patterns_match_ci_workflow() -> None:
    """_DOC_PATTERNS must equal the `case` globs in the ci.yml `changes` job.

    Broadening one without the other silently disables the gate: the widened list
    reclassifies code files as doc-only, so the check prints OK and exits 0 while
    letting a fragment-less change merge.
    """
    ci_yml = (Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    match = re.search(r"^\s*(\S+)\)\s*;;\s*$", ci_yml, re.MULTILINE)
    assert match is not None, "could not locate the doc-only `case` arm in ci.yml"

    ci_patterns = tuple(match.group(1).split("|"))

    assert ci_patterns == _check._DOC_PATTERNS
