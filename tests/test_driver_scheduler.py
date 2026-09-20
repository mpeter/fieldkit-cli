"""Tests for fieldkit.driver.scheduler — covers-as-locks selection.

Spec: openspec/specs/driver-scheduling/spec.md
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_CANONICAL_SPEC = Path(__file__).parents[1] / "openspec/specs/driver-scheduling/spec.md"


def test_scheduler_contract_is_public_and_canonical() -> None:
    """The implementation must not point contributors at private change history."""
    spec = _CANONICAL_SPEC.read_text(encoding="utf-8")
    scheduler = (Path(__file__).parents[1] / "src/fieldkit/driver/scheduler.py").read_text(encoding="utf-8")

    assert "Requirement: The driver MUST NOT start a work order whose covers intersect" in spec
    assert "Requirement: The driver MUST honor `depends_on` frontmatter" in spec
    assert "Requirement: Concurrent work orders MUST have pairwise-disjoint covers" in spec
    assert "Requirement: The work-order execution agent MUST resolve edit sites" in spec
    assert "Spec: openspec/specs/driver-scheduling/spec.md" in scheduler
    assert "openspec/changes/" not in scheduler
    assert __doc__ is not None
    assert "openspec/changes/" not in __doc__


def test_scheduler_error_inherits_from_fieldkit_error() -> None:
    from fieldkit.driver.scheduler import SchedulerError
    from fieldkit.errors import FieldkitError

    error = SchedulerError("failure")
    assert isinstance(error, FieldkitError)


def _write_wo(tmp_path: Path, name: str, frontmatter: str, body: str = "# Work Order\n") -> Path:
    path = tmp_path / name
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
    return path


def _issue(number: int):
    from fieldkit.driver.github import AgentIssue

    return AgentIssue(number=number, title=f"Issue {number}", body="", labels=["agent-ready"])


# ---------------------------------------------------------------------------
# parse_covers
# ---------------------------------------------------------------------------


def test_parse_covers_returns_listed_paths(tmp_path: Path) -> None:
    from fieldkit.driver.scheduler import parse_covers

    wo = _write_wo(
        tmp_path,
        "wo.md",
        "last_reviewed: 2026-07-16\ncovers:\n  - src/fieldkit/sf/client.py\n  - tests/test_sf_client.py\naudience: developer",
    )

    result = parse_covers(wo)

    assert result == frozenset({"src/fieldkit/sf/client.py", "tests/test_sf_client.py"})


def test_parse_covers_dedupes_duplicate_entries(tmp_path: Path) -> None:
    from fieldkit.driver.scheduler import parse_covers

    wo = _write_wo(tmp_path, "wo.md", "covers:\n  - src/a.py\n  - src/a.py")

    result = parse_covers(wo)

    assert result == frozenset({"src/a.py"})


@pytest.mark.parametrize(
    "frontmatter",
    [
        pytest.param("last_reviewed: 2026-07-16\naudience: developer", id="no-covers-key"),
        pytest.param("covers:\naudience: developer", id="empty-covers-list"),
    ],
)
def test_parse_covers_returns_none_when_absent_or_empty(tmp_path: Path, frontmatter: str) -> None:
    from fieldkit.driver.scheduler import parse_covers

    wo = _write_wo(tmp_path, "wo.md", frontmatter)

    result = parse_covers(wo)

    assert result is None


def test_parse_covers_returns_none_without_frontmatter(tmp_path: Path) -> None:
    from fieldkit.driver.scheduler import parse_covers

    plain = tmp_path / "tasks.md"
    plain.write_text("# Tasks\n\n- [ ] 1.1 Do the thing\n", encoding="utf-8")

    result = parse_covers(plain)

    assert result is None


def test_parse_covers_returns_none_for_missing_file(tmp_path: Path) -> None:
    from fieldkit.driver.scheduler import parse_covers

    result = parse_covers(tmp_path / "does-not-exist.md")

    assert result is None


# ---------------------------------------------------------------------------
# parse_depends_on
# ---------------------------------------------------------------------------


def test_parse_depends_on_inline_list(tmp_path: Path) -> None:
    from fieldkit.driver.scheduler import parse_depends_on

    wo = _write_wo(tmp_path, "wo.md", "covers:\n  - src/a.py\ndepends_on: [1293, 1300]")

    result = parse_depends_on(wo)

    assert result == (1293, 1300)


def test_parse_depends_on_block_list_with_hash_prefixes(tmp_path: Path) -> None:
    from fieldkit.driver.scheduler import parse_depends_on

    wo = _write_wo(tmp_path, "wo.md", 'depends_on:\n  - "#1293"\n  - 42')

    result = parse_depends_on(wo)

    assert result == (1293, 42)


def test_parse_depends_on_dedupes_and_preserves_order(tmp_path: Path) -> None:
    from fieldkit.driver.scheduler import parse_depends_on

    wo = _write_wo(tmp_path, "wo.md", "depends_on: [7, 3, 7]")

    result = parse_depends_on(wo)

    assert result == (7, 3)


@pytest.mark.parametrize(
    "frontmatter",
    [
        pytest.param("covers:\n  - src/a.py", id="absent"),
        pytest.param("depends_on: []", id="empty-inline"),
        pytest.param("depends_on: [not-a-number]", id="non-numeric-item"),
    ],
)
def test_parse_depends_on_empty_cases(tmp_path: Path, frontmatter: str) -> None:
    from fieldkit.driver.scheduler import parse_depends_on

    wo = _write_wo(tmp_path, "wo.md", frontmatter)

    result = parse_depends_on(wo)

    assert result == ()


# ---------------------------------------------------------------------------
# select_runnable — pure selection logic
# ---------------------------------------------------------------------------


def test_select_runnable_blocks_candidate_whose_covers_are_busy(tmp_path: Path) -> None:
    from fieldkit.driver.scheduler import select_runnable

    wo = _write_wo(tmp_path, "wo-a.md", "covers:\n  - src/fieldkit/sf/client.py")
    busy = {"src/fieldkit/sf/client.py": 1350}

    result = select_runnable([(_issue(1), wo)], busy, frozenset(), max_concurrent=1)

    selected, skips = result
    assert selected == []
    assert len(skips) == 1
    assert skips[0].kind == "busy-file"
    assert skips[0].issue_number == 1
    assert "src/fieldkit/sf/client.py" in skips[0].detail
    assert "#1350" in skips[0].detail


def test_select_runnable_directory_cover_overlaps_busy_file_beneath_it(tmp_path: Path) -> None:
    from fieldkit.driver.scheduler import select_runnable

    wo = _write_wo(tmp_path, "wo-a.md", "covers:\n  - docs/work-orders/")
    busy = {"docs/work-orders/implementation change.md": 1351}

    result = select_runnable([(_issue(1), wo)], busy, frozenset(), max_concurrent=1)

    selected, skips = result
    assert selected == []
    assert skips[0].kind == "busy-file"


def test_select_runnable_blocks_open_dependency(tmp_path: Path) -> None:
    from fieldkit.driver.scheduler import select_runnable

    wo = _write_wo(tmp_path, "wo-b.md", "covers:\n  - src/b.py\ndepends_on: [1293]")

    result = select_runnable([(_issue(2), wo)], {}, frozenset({1293}), max_concurrent=1)

    selected, skips = result
    assert selected == []
    assert skips[0].kind == "depends-open"
    assert "#1293" in skips[0].detail


def test_select_runnable_allows_closed_dependency(tmp_path: Path) -> None:
    from fieldkit.driver.scheduler import select_runnable

    wo = _write_wo(tmp_path, "wo-b.md", "covers:\n  - src/b.py\ndepends_on: [1293]")

    result = select_runnable([(_issue(2), wo)], {}, frozenset(), max_concurrent=1)

    selected, skips = result
    assert [issue.number for issue, _ in selected] == [2]
    assert skips == []


def test_select_runnable_batch_overlap_third_issue_waits(tmp_path: Path) -> None:
    # Spec scenario: A, B, C oldest-first; C overlaps A → A and B run, C skipped.
    from fieldkit.driver.scheduler import select_runnable

    wo_a = _write_wo(tmp_path, "wo-a.md", "covers:\n  - src/a.py")
    wo_b = _write_wo(tmp_path, "wo-b.md", "covers:\n  - src/b.py")
    wo_c = _write_wo(tmp_path, "wo-c.md", "covers:\n  - src/a.py\n  - src/c.py")
    candidates = [(_issue(1), wo_a), (_issue(2), wo_b), (_issue(3), wo_c)]

    result = select_runnable(candidates, {}, frozenset(), max_concurrent=3)

    selected, skips = result
    assert [issue.number for issue, _ in selected] == [1, 2]
    assert len(skips) == 1
    assert skips[0].issue_number == 3
    assert skips[0].kind == "batch-overlap"
    assert "#1" in skips[0].detail


def test_select_runnable_caps_batch_at_max_concurrent(tmp_path: Path) -> None:
    from fieldkit.driver.scheduler import select_runnable

    candidates = [(_issue(n), _write_wo(tmp_path, f"wo-{n}.md", f"covers:\n  - src/f{n}.py")) for n in (1, 2, 3, 4)]

    result = select_runnable(candidates, {}, frozenset(), max_concurrent=2)

    selected, skips = result
    assert [issue.number for issue, _ in selected] == [1, 2]
    # Candidates beyond capacity are not skip-logged — they simply wait.
    assert skips == []


def test_select_runnable_preserves_oldest_first_priority(tmp_path: Path) -> None:
    # Oldest candidate blocked → next eligible runs; order never rearranged.
    from fieldkit.driver.scheduler import select_runnable

    wo_a = _write_wo(tmp_path, "wo-a.md", "covers:\n  - src/a.py")
    wo_b = _write_wo(tmp_path, "wo-b.md", "covers:\n  - src/b.py")
    busy = {"src/a.py": 1340}

    result = select_runnable([(_issue(1), wo_a), (_issue(2), wo_b)], busy, frozenset(), max_concurrent=1)

    selected, skips = result
    assert [issue.number for issue, _ in selected] == [2]
    assert skips[0].issue_number == 1


def test_select_runnable_no_covers_runs_only_in_isolation(tmp_path: Path) -> None:
    from fieldkit.driver.scheduler import select_runnable

    tasks = tmp_path / "tasks.md"
    tasks.write_text("# Tasks\n", encoding="utf-8")  # OpenSpec-style: no frontmatter

    result = select_runnable([(_issue(5), tasks)], {}, frozenset(), max_concurrent=2)

    selected, skips = result
    assert [issue.number for issue, _ in selected] == [5]
    assert skips == []


def test_select_runnable_no_covers_blocked_by_any_busy_file(tmp_path: Path) -> None:
    from fieldkit.driver.scheduler import select_runnable

    tasks = tmp_path / "tasks.md"
    tasks.write_text("# Tasks\n", encoding="utf-8")
    busy = {"docs/unrelated.md": 1300}

    result = select_runnable([(_issue(5), tasks)], busy, frozenset(), max_concurrent=2)

    selected, skips = result
    assert selected == []
    assert skips[0].kind == "no-covers"
    assert "#1300" in skips[0].detail


def test_select_runnable_no_covers_never_joins_a_batch(tmp_path: Path) -> None:
    from fieldkit.driver.scheduler import select_runnable

    wo_a = _write_wo(tmp_path, "wo-a.md", "covers:\n  - src/a.py")
    tasks = tmp_path / "tasks.md"
    tasks.write_text("# Tasks\n", encoding="utf-8")

    result = select_runnable([(_issue(1), wo_a), (_issue(2), tasks)], {}, frozenset(), max_concurrent=2)

    selected, skips = result
    assert [issue.number for issue, _ in selected] == [1]
    assert skips[0].issue_number == 2
    assert skips[0].kind == "no-covers"


def test_select_runnable_universal_first_blocks_everything_after(tmp_path: Path) -> None:
    from fieldkit.driver.scheduler import select_runnable

    tasks = tmp_path / "tasks.md"
    tasks.write_text("# Tasks\n", encoding="utf-8")
    wo_b = _write_wo(tmp_path, "wo-b.md", "covers:\n  - src/b.py")

    result = select_runnable([(_issue(1), tasks), (_issue(2), wo_b)], {}, frozenset(), max_concurrent=2)

    selected, skips = result
    assert [issue.number for issue, _ in selected] == [1]
    assert skips[0].issue_number == 2
    assert skips[0].kind == "batch-overlap"


# ---------------------------------------------------------------------------
# busy_files — gh boundary, fail closed
# ---------------------------------------------------------------------------


def _gh_ok(stdout: str) -> MagicMock:
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def test_busy_files_maps_paths_to_pr_numbers() -> None:
    from fieldkit.driver.scheduler import busy_files

    payload = json.dumps(
        [
            {"number": 1350, "files": [{"path": "src/a.py"}, {"path": "src/b.py"}]},
            {"number": 1351, "files": [{"path": "src/c.py"}]},
        ]
    )
    with patch("fieldkit.driver.scheduler.subprocess.run", return_value=_gh_ok(payload)) as mock_run:
        result = busy_files("owner/repo")

    assert result == {"src/a.py": 1350, "src/b.py": 1350, "src/c.py": 1351}
    assert mock_run.call_count == 1  # one gh call for the whole busy set


def test_busy_files_raises_scheduler_error_on_gh_failure() -> None:
    from fieldkit.driver.scheduler import SchedulerError, busy_files

    failed = MagicMock(returncode=1, stdout="", stderr="boom")
    with (
        patch("fieldkit.driver.scheduler.subprocess.run", return_value=failed),
        pytest.raises(SchedulerError, match="gh pr failed"),
    ):
        busy_files("owner/repo")


@pytest.mark.parametrize(
    "raised",
    [
        subprocess.TimeoutExpired(cmd=["gh"], timeout=30),
        FileNotFoundError("gh not on PATH"),
    ],
)
def test_busy_files_raises_scheduler_error_on_timeout_or_oserror(raised: Exception) -> None:
    # Fail closed: a hung or missing gh binary must surface as SchedulerError
    # so run_driver records outcome=skipped instead of crashing the tick.
    from fieldkit.driver.scheduler import SchedulerError, busy_files

    with (
        patch("fieldkit.driver.scheduler.subprocess.run", side_effect=raised),
        pytest.raises(SchedulerError, match="gh pr failed"),
    ):
        busy_files("owner/repo")


def test_busy_files_raises_scheduler_error_on_bad_json() -> None:
    from fieldkit.driver.scheduler import SchedulerError, busy_files

    with (
        patch("fieldkit.driver.scheduler.subprocess.run", return_value=_gh_ok("not json")),
        pytest.raises(SchedulerError, match="unparseable JSON"),
    ):
        busy_files("owner/repo")


def test_busy_files_paginates_prs_at_the_files_cap() -> None:
    # A PR reporting exactly 100 files may be truncated by the GraphQL files
    # connection — the complete set must come from the paginated REST endpoint.
    from fieldkit.driver.scheduler import busy_files

    capped_files = [{"path": f"src/f{i}.py"} for i in range(100)]
    listing = json.dumps([{"number": 1360, "files": capped_files}])
    rest_page = json.dumps([[{"filename": f"src/f{i}.py"} for i in range(150)]])

    with patch(
        "fieldkit.driver.scheduler.subprocess.run",
        side_effect=[_gh_ok(listing), _gh_ok(rest_page)],
    ) as mock_run:
        result = busy_files("owner/repo")

    assert len(result) == 150
    assert result["src/f149.py"] == 1360
    assert mock_run.call_count == 2
    rest_call_args = mock_run.call_args_list[1].args[0]
    assert "--paginate" in rest_call_args


# ---------------------------------------------------------------------------
# open_issue_numbers — fail closed on nonexistent issues
# ---------------------------------------------------------------------------


def test_open_issue_numbers_partitions_open_and_closed() -> None:
    from fieldkit.driver.scheduler import open_issue_numbers

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        number = int(cmd[-1].rsplit("/", 1)[-1])
        state = "closed" if number == 100 else "open"
        return _gh_ok(json.dumps({"number": number, "state": state}))

    with patch("fieldkit.driver.scheduler.subprocess.run", side_effect=fake_run):
        result = open_issue_numbers("owner/repo", [100, 200])

    assert result == frozenset({200})


def test_open_issue_numbers_treats_nonexistent_issue_as_open() -> None:
    from fieldkit.driver.scheduler import open_issue_numbers

    not_found = MagicMock(returncode=1, stdout="", stderr="HTTP 404: Not Found")
    with patch("fieldkit.driver.scheduler.subprocess.run", return_value=not_found):
        result = open_issue_numbers("owner/repo", [9999])

    assert result == frozenset({9999})


def test_open_issue_numbers_empty_input_makes_no_gh_calls() -> None:
    from fieldkit.driver.scheduler import open_issue_numbers

    with patch("fieldkit.driver.scheduler.subprocess.run") as mock_run:
        result = open_issue_numbers("owner/repo", [])

    assert result == frozenset()
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# get_driver_max_concurrent — config accessor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_config", "expected"),
    [
        pytest.param(None, 1, id="no-config"),
        pytest.param({}, 1, id="no-driver-section"),
        pytest.param({"driver": {"max_concurrent": 2}}, 2, id="in-range"),
        pytest.param({"driver": {"max_concurrent": 0}}, 1, id="clamped-low"),
        pytest.param({"driver": {"max_concurrent": 99}}, 4, id="clamped-high"),
        pytest.param({"driver": {"max_concurrent": "not-a-number"}}, 1, id="non-integer"),
        pytest.param({"driver": "not-a-dict"}, 1, id="malformed-section"),
    ],
)
def test_get_driver_max_concurrent(raw_config: object, expected: int) -> None:
    from fieldkit.config._loader import clear_config_caches
    from fieldkit.config._settings import get_driver_max_concurrent

    clear_config_caches()
    try:
        with patch("fieldkit.config._loader._load_raw_config", return_value=raw_config):
            result = get_driver_max_concurrent()
    finally:
        clear_config_caches()

    assert result == expected
