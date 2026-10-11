"""Tests for the exit-site ratchet in scripts/check_exit_sites.py."""

import json
import re
from pathlib import Path

import pytest

from scripts import check_exit_sites

pytestmark = pytest.mark.unit


def _module(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("raise SystemExit(3)\n", [1]),
        ("raise SystemExit\n", [1]),
        ("import click\nraise click.exceptions.Exit(0)\n", [2]),
        ("import sys\nsys.exit(1)\n", [2]),
        ("def f(ctx):\n    ctx.exit(1)\n", [2]),
        ("import os\nos._exit(1)\n", [2]),
        ("exit(1)\n", [1]),
        ("raise ValueError('bad')\n", []),
        ("if __name__ == '__main__':\n    raise SystemExit(main())\n", []),
    ],
)
def test_exit_site_lines_counts_every_exit_form_outside_a_main_guard(source: str, expected: list[int]) -> None:
    assert check_exit_sites.exit_site_lines(source) == expected


def test_collect_skips_the_two_process_boundaries(tmp_path: Path) -> None:
    _module(tmp_path, "cli_exit.py", "import sys\nsys.exit(3)\n")
    _module(tmp_path, "__main__.py", "import sys\nsys.exit(0)\n")
    _module(tmp_path, "commands/demo.py", "raise SystemExit(3)\n")

    sites = check_exit_sites.collect(tmp_path)

    assert sites == {"commands/demo.py": [1]}


def test_violations_pass_when_every_file_matches_its_baseline() -> None:
    problems = check_exit_sites.violations({"commands/demo.py": [4, 9]}, {"commands/demo.py": 2})

    assert problems == []


@pytest.mark.parametrize(
    ("sites", "baseline", "message"),
    [
        ({"commands/demo.py": [4, 9]}, {"commands/demo.py": 1}, r"commands/demo\.py:4,9: 2 exit sites, baseline 1"),
        ({"commands/new.py": [7]}, {}, r"commands/new\.py:7: 1 exit sites, baseline 0"),
        ({"pursuit/io.py": [12]}, {}, r"pursuit/io\.py:12: domain code must raise a typed FieldkitError"),
        ({"commands/demo.py": [4]}, {"commands/demo.py": 2}, r"commands/demo\.py: 1 exit sites, baseline 2; lower"),
        ({}, {"commands/gone.py": 1}, r"commands/gone\.py: 0 exit sites, baseline 1; lower"),
    ],
    ids=["added-site", "new-file", "domain-site", "removed-site", "file-cleared"],
)
def test_violations_reject_growth_domain_exits_and_stale_baselines(
    sites: dict[str, list[int]], baseline: dict[str, int], message: str
) -> None:
    problems = check_exit_sites.violations(sites, baseline)

    assert len(problems) == 1
    assert re.search(message, problems[0])


def test_write_baseline_refuses_to_record_a_new_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = tmp_path / "baseline.json"
    check_exit_sites.write_baseline({"commands/demo.py": [3]}, baseline)
    _module(tmp_path / "src", "commands/demo.py", "raise SystemExit(1)\nraise SystemExit(3)\n")
    monkeypatch.setattr(check_exit_sites, "SOURCE_ROOT", tmp_path / "src")
    monkeypatch.setattr(check_exit_sites, "BASELINE_PATH", baseline)

    exit_code = check_exit_sites.main(["--write-baseline"])

    assert exit_code == 1
    assert check_exit_sites.load_baseline(baseline) == {"commands/demo.py": 1}


def test_write_baseline_lowers_counts_after_a_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = tmp_path / "baseline.json"
    check_exit_sites.write_baseline({"commands/demo.py": [3, 5], "commands/done.py": [1]}, baseline)
    _module(tmp_path / "src", "commands/demo.py", "raise SystemExit(1)\n")
    _module(tmp_path / "src", "commands/done.py", "raise ValueError('typed now')\n")
    monkeypatch.setattr(check_exit_sites, "SOURCE_ROOT", tmp_path / "src")
    monkeypatch.setattr(check_exit_sites, "BASELINE_PATH", baseline)

    exit_code = check_exit_sites.main(["--write-baseline"])

    assert exit_code == 0
    assert json.loads(baseline.read_text(encoding="utf-8"))["files"] == {"commands/demo.py": 1}


def test_repository_exit_sites_match_the_committed_baseline() -> None:
    """No module gains an exit site and every removal lowers the committed baseline."""
    problems = check_exit_sites.violations(check_exit_sites.collect(), check_exit_sites.load_baseline())

    assert problems == []
