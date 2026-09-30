"""Tests for fieldkit.driver.scheduler — covers-as-locks selection.

Spec: openspec/specs/driver-scheduling/spec.md
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from fieldkit.driver.prompt_contract import EditSite, PromptContract, PromptKind
from fieldkit.driver.prompt_source import FrozenPrompt, PromptSource
from fieldkit.util.bounded_process import BoundedProcessResult

pytestmark = pytest.mark.unit

_CANONICAL_SPEC = Path(__file__).parents[1] / "openspec/specs/driver-scheduling/spec.md"


def test_scheduler_contract_is_public_and_canonical() -> None:
    """The implementation must not point contributors at private change history."""
    spec = _CANONICAL_SPEC.read_text(encoding="utf-8")
    scheduler = (Path(__file__).parents[1] / "src/fieldkit/driver/scheduler.py").read_text(encoding="utf-8")

    assert "Requirement: The driver MUST NOT start a work order whose covers intersect" in spec
    assert "Requirement: The driver MUST honor `depends_on` frontmatter" in spec
    assert "Requirement: Concurrent work orders MUST have pairwise-disjoint covers" in spec
    assert "Requirement: The driver MUST validate structured edit sites" in spec
    assert "Requirement: Driver execution MUST use a packaged portable instruction source" in spec
    assert "Spec: openspec/specs/driver-scheduling/spec.md" in scheduler
    assert "openspec/changes/" not in scheduler
    assert __doc__ is not None
    assert "openspec/changes/" not in __doc__


def test_scheduler_error_inherits_from_fieldkit_error() -> None:
    from fieldkit.driver.scheduler import SchedulerError
    from fieldkit.errors import FieldkitError

    error = SchedulerError("failure")
    assert isinstance(error, FieldkitError)


def _write_wo(
    tmp_path: Path,
    name: str,
    frontmatter: str,
    body: str = "# Work Order\n",
    *,
    kind: PromptKind = "work-order",
) -> FrozenPrompt:
    path = tmp_path / name
    del body
    parsed = yaml.safe_load(frontmatter)
    covers = frozenset(parsed["covers"])
    depends_on = tuple(int(str(item).removeprefix("#")) for item in parsed.get("depends_on", []))
    target = sorted(covers)[0]
    if target.endswith("/"):
        target = f"{target}example.py"
    return FrozenPrompt(
        PromptSource(path, kind),
        "a" * 40,
        PromptContract(covers, depends_on, (EditSite(target, "unique anchor"),)),
    )


def _issue(number: int):
    from fieldkit.driver.github import AgentIssue

    return AgentIssue(number=number, title=f"Issue {number}", body="", labels=["agent-ready"])


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


def test_select_runnable_allows_disjoint_openspec_contracts(tmp_path: Path) -> None:
    from fieldkit.driver.scheduler import select_runnable

    prompt_a = _write_wo(tmp_path, "change-a/tasks.md", "covers:\n  - src/a.py", kind="openspec")
    prompt_b = _write_wo(tmp_path, "change-b/tasks.md", "covers:\n  - src/b.py", kind="openspec")

    result = select_runnable([(_issue(1), prompt_a), (_issue(2), prompt_b)], {}, frozenset(), max_concurrent=2)

    selected, skips = result
    assert [issue.number for issue, _ in selected] == [1, 2]
    assert skips == []


# ---------------------------------------------------------------------------
# busy_files — gh boundary, fail closed
# ---------------------------------------------------------------------------


def test_busy_files_rejects_potentially_truncated_pr_listing() -> None:
    from fieldkit.driver.scheduler import SchedulerError, busy_files

    listing = [{"number": number, "files": []} for number in range(1, 201)]
    with (
        patch("fieldkit.driver.scheduler._gh_json", return_value=listing),
        pytest.raises(SchedulerError, match="PR listing may be truncated"),
    ):
        busy_files("owner/repo")


@pytest.mark.parametrize("entry", [None, {}, {"path": 1}, {"path": ""}])
def test_busy_files_rejects_malformed_file_entry(entry: object) -> None:
    from fieldkit.driver.scheduler import SchedulerError, busy_files

    with (
        patch("fieldkit.driver.scheduler._gh_json", return_value=[{"number": 1, "files": [entry]}]),
        pytest.raises(SchedulerError, match="invalid file entry"),
    ):
        busy_files("owner/repo")


@pytest.mark.parametrize("entry", [None, {}, {"filename": 1}, {"filename": ""}])
def test_busy_files_rejects_malformed_paginated_file_entry(entry: object) -> None:
    from fieldkit.driver.scheduler import SchedulerError, busy_files

    listing = [{"number": 1, "files": [{"path": f"src/file-{index}.py"} for index in range(100)]}]
    with (
        patch("fieldkit.driver.scheduler._gh_json", side_effect=[listing, [[entry]]]),
        pytest.raises(SchedulerError, match="invalid file entry"),
    ):
        busy_files("owner/repo")


@pytest.mark.parametrize("count", [2999, 3000, 3001])
def test_busy_files_checks_paginated_endpoint_limit(count: int) -> None:
    from fieldkit.driver.scheduler import SchedulerError, busy_files

    listing = [{"number": 1, "files": [{"path": f"src/file-{index}.py"} for index in range(100)]}]
    pages = [[{"filename": f"src/file-{index}.py"} for index in range(count)]]
    with patch("fieldkit.driver.scheduler._gh_json", side_effect=[listing, pages]):
        if count < 3000:
            result = busy_files("owner/repo")
            assert len(result) == count
            assert result["src/file-2998.py"] == 1
        else:
            with pytest.raises(SchedulerError, match="file listing may be truncated"):
                busy_files("owner/repo")


@pytest.mark.parametrize("start,count", [(0, 0), (0, 99), (1, 100)])
def test_busy_files_rejects_inconsistent_paginated_paths(start: int, count: int) -> None:
    from fieldkit.driver.scheduler import SchedulerError, busy_files

    listing = [{"number": 1, "files": [{"path": f"src/file-{index}.py"} for index in range(100)]}]
    pages = [[{"filename": f"src/file-{index}.py"} for index in range(start, start + count)]]
    with (
        patch("fieldkit.driver.scheduler._gh_json", side_effect=[listing, pages]),
        pytest.raises(SchedulerError, match="file listings disagree"),
    ):
        busy_files("owner/repo")


def _gh_ok(stdout: str) -> BoundedProcessResult:
    return BoundedProcessResult(returncode=0, stdout=stdout, stderr="")


def test_busy_files_maps_paths_to_pr_numbers() -> None:
    from fieldkit.driver.scheduler import busy_files

    payload = json.dumps(
        [
            {"number": 1350, "files": [{"path": "src/a.py"}, {"path": "src/b.py"}]},
            {"number": 1351, "files": [{"path": "src/c.py"}]},
        ]
    )
    with patch("fieldkit.driver.scheduler.run_bounded_process", return_value=_gh_ok(payload)) as mock_run:
        result = busy_files("owner/repo")

    assert result == {"src/a.py": 1350, "src/b.py": 1350, "src/c.py": 1351}
    assert mock_run.call_count == 1  # one gh call for the whole busy set


def test_busy_files_raises_provider_error_on_gh_failure() -> None:
    from fieldkit.driver.scheduler import busy_files
    from fieldkit.errors import GitHubRequestError

    failed = _gh_ok("")
    failed = type(failed)(returncode=1, stdout="", stderr="fictional-secret")
    with (
        patch("fieldkit.driver.scheduler.run_bounded_process", return_value=failed),
        pytest.raises(GitHubRequestError, match="scheduling lookup failed") as caught,
    ):
        busy_files("owner/repo")
    assert "fictional-secret" not in str(caught.value)


@pytest.mark.parametrize(
    "message",
    ["driver support process timed out", "driver support process could not start"],
)
def test_busy_files_raises_provider_error_on_timeout_or_oserror(message: str) -> None:
    # Fail closed: a hung or missing gh binary must surface as SchedulerError
    # so run_driver records outcome=skipped instead of crashing the tick.
    from fieldkit.driver.scheduler import busy_files
    from fieldkit.errors import GitHubRequestError
    from fieldkit.util.bounded_process import BoundedProcessError

    with (
        patch(
            "fieldkit.driver.scheduler.run_bounded_process",
            side_effect=BoundedProcessError(message, reason="timeout"),
        ),
        pytest.raises(GitHubRequestError, match="did not complete"),
    ):
        busy_files("owner/repo")


@pytest.mark.parametrize(
    ("stderr", "error_type", "message"),
    [
        pytest.param(
            "authentication required: fictional-secret /private/path",
            "auth",
            "authentication failed",
            id="authentication",
        ),
        pytest.param(
            "HTTP 403: API rate limit exceeded; fictional-secret /private/path",
            "provider",
            "lookup failed",
            id="rate-limit",
        ),
    ],
)
def test_busy_files_preserves_github_failure_taxonomy_without_payload(
    stderr: str, error_type: str, message: str
) -> None:
    from fieldkit.driver.scheduler import busy_files
    from fieldkit.errors import AuthError, GitHubRequestError

    expected = AuthError if error_type == "auth" else GitHubRequestError
    failure = BoundedProcessResult(1, "partial output", stderr)
    with (
        patch("fieldkit.driver.scheduler.run_bounded_process", return_value=failure),
        pytest.raises(expected, match=message) as caught,
    ):
        busy_files("owner/repo")

    assert "fictional-secret" not in str(caught.value)
    assert "/private/path" not in str(caught.value)


def test_busy_files_raises_scheduler_error_on_bad_json() -> None:
    from fieldkit.driver.scheduler import SchedulerError, busy_files

    with (
        patch("fieldkit.driver.scheduler.run_bounded_process", return_value=_gh_ok("not json")),
        pytest.raises(SchedulerError, match="invalid JSON"),
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
        "fieldkit.driver.scheduler.run_bounded_process",
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

    def fake_run(cmd: list[str], **kwargs: object) -> BoundedProcessResult:
        number = int(cmd[-1].rsplit("/", 1)[-1])
        state = "closed" if number == 100 else "open"
        return _gh_ok(json.dumps({"number": number, "state": state}))

    with patch("fieldkit.driver.scheduler.run_bounded_process", side_effect=fake_run):
        result = open_issue_numbers("owner/repo", [100, 200])

    assert result == frozenset({200})


def test_open_issue_numbers_treats_nonexistent_issue_as_open() -> None:
    from fieldkit.driver.scheduler import open_issue_numbers

    not_found = type(_gh_ok(""))(returncode=1, stdout="", stderr="HTTP 404: Not Found")
    with patch("fieldkit.driver.scheduler.run_bounded_process", return_value=not_found):
        result = open_issue_numbers("owner/repo", [9999])

    assert result == frozenset({9999})


def test_open_issue_numbers_empty_input_makes_no_gh_calls() -> None:
    from fieldkit.driver.scheduler import open_issue_numbers

    with patch("fieldkit.driver.scheduler.run_bounded_process") as mock_run:
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
