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
        ("raise SystemExit(3)\n", {"<module>": [1]}),
        ("raise SystemExit\n", {"<module>": [1]}),
        ("import click\nraise click.exceptions.Exit(0)\n", {"<module>": [2]}),
        ("import sys\nsys.exit(1)\n", {"<module>": [2]}),
        ("def cli(ctx):\n    ctx.exit(1)\n", {"cli": [2]}),
        ("import os\nos._exit(1)\n", {"<module>": [2]}),
        ("exit(1)\n", {"<module>": [1]}),
        ("class Run:\n    def go(self):\n        raise SystemExit(3)\n", {"Run.go": [3]}),
        ("raise ValueError('bad')\n", {}),
        ("if __name__ == '__main__':\n    raise SystemExit(main())\n", {}),
        ("if __name__ == '__main__':\n    main()\nelse:\n    raise SystemExit(3)\n", {"<module>": [4]}),
        ("from sys import exit as terminate\nterminate(3)\n", {"<module>": [2]}),
        ("from os import _exit as hard_stop\nhard_stop(1)\n", {"<module>": [2]}),
        ("from click.exceptions import Exit as Done\nraise Done(0)\n", {"<module>": [2]}),
        ("from builtins import SystemExit as Stop\nraise Stop\n", {"<module>": [2]}),
    ],
)
def test_exit_sites_counts_every_exit_form_by_enclosing_function(source: str, expected: dict[str, list[int]]) -> None:
    assert check_exit_sites.exit_sites(source) == expected


@pytest.mark.parametrize(
    "guard",
    ["if __name__ != '__main__':", "if __name__ == PLUGIN_NAME:", "if __name__ == '__main__' == True:"],
)
def test_exit_sites_exempt_only_the_exact_main_guard(guard: str) -> None:
    assert check_exit_sites.exit_sites(f"{guard}\n    raise SystemExit(3)\n") == {"<module>": [2]}


def test_collect_exempts_only_the_two_boundary_functions(tmp_path: Path) -> None:
    _module(tmp_path, "cli_exit.py", "import sys\ndef cli_main():\n    sys.exit(3)\ndef helper():\n    sys.exit(1)\n")
    _module(tmp_path, "__main__.py", "def main():\n    raise SystemExit(0)\n")
    _module(tmp_path, "commands/demo.py", "def cli():\n    raise SystemExit(3)\n")

    sites = check_exit_sites.collect(tmp_path)

    assert sites == {"cli_exit.py": {"helper": [5]}, "commands/demo.py": {"cli": [2]}}
    assert "domain code must raise" in check_exit_sites.violations(sites, {"commands/demo.py": {"cli": 1}})[0]


def test_violations_pass_when_every_function_matches_its_baseline() -> None:
    problems = check_exit_sites.violations({"commands/demo.py": {"cli": [4, 9]}}, {"commands/demo.py": {"cli": 2}})

    assert problems == []


@pytest.mark.parametrize(
    ("sites", "baseline", "messages"),
    [
        (
            {"commands/demo.py": {"cli": [4, 9]}},
            {"commands/demo.py": {"cli": 1}},
            [r"commands/demo\.py:4,9 \(cli\): 2 exit sites, baseline 1"],
        ),
        ({"commands/new.py": {"cli": [7]}}, {}, [r"commands/new\.py:7 \(cli\): 1 exit sites, baseline 0"]),
        ({"pursuit/io.py": {"<module>": [12]}}, {}, [r"pursuit/io\.py:12 \(<module>\): domain code must raise"]),
        (
            {"commands/demo.py": {"cli": [4]}},
            {"commands/demo.py": {"cli": 2}},
            [r"commands/demo\.py \(cli\): 1 exit sites, baseline 2; lower"],
        ),
        ({}, {"commands/gone.py": {"cli": 1}}, [r"commands/gone\.py \(cli\): 0 exit sites, baseline 1; lower"]),
        (
            {"commands/demo.py": {"other": [20]}},
            {"commands/demo.py": {"cli": 1}},
            [r"commands/demo\.py:20 \(other\): 1 exit sites, baseline 0", r"commands/demo\.py \(cli\): 0 exit sites"],
        ),
    ],
    ids=["added-site", "new-file", "domain-site", "removed-site", "file-cleared", "site-moved-in-file"],
)
def test_violations_reject_growth_domain_exits_moves_and_stale_baselines(
    sites: check_exit_sites.Sites, baseline: check_exit_sites.Baseline, messages: list[str]
) -> None:
    problems = check_exit_sites.violations(sites, baseline)

    assert len(problems) == len(messages)
    for problem, message in zip(problems, messages, strict=True):
        assert re.search(message, problem)


def _patch_paths(monkeypatch: pytest.MonkeyPatch, source_root: Path, baseline: Path) -> None:
    monkeypatch.setattr(check_exit_sites, "SOURCE_ROOT", source_root)
    monkeypatch.setattr(check_exit_sites, "BASELINE_PATH", baseline)


def test_write_baseline_refuses_to_record_a_larger_file_total(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = tmp_path / "baseline.json"
    check_exit_sites.write_baseline({"commands/demo.py": {"cli": [2]}}, baseline)
    _module(tmp_path / "src", "commands/demo.py", "def cli():\n    raise SystemExit(1)\n    raise SystemExit(3)\n")
    _patch_paths(monkeypatch, tmp_path / "src", baseline)

    exit_code = check_exit_sites.main(["--write-baseline"])

    assert exit_code == 1
    assert check_exit_sites.load_baseline(baseline) == {"commands/demo.py": {"cli": 1}}


def test_write_baseline_refuses_to_record_a_domain_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = tmp_path / "baseline.json"
    check_exit_sites.write_baseline({}, baseline)
    _module(tmp_path / "src", "pursuit/io.py", "import sys\nsys.exit(1)\n")
    _patch_paths(monkeypatch, tmp_path / "src", baseline)

    exit_code = check_exit_sites.main(["--write-baseline"])

    assert exit_code == 1
    assert check_exit_sites.load_baseline(baseline) == {}


def test_write_baseline_records_removals_and_moves_within_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = tmp_path / "baseline.json"
    check_exit_sites.write_baseline(
        {"commands/demo.py": {"cli": [3, 5]}, "commands/done.py": {"cli": [1]}},
        baseline,
    )
    _module(tmp_path / "src", "commands/demo.py", "def helper():\n    raise SystemExit(1)\n")
    _module(tmp_path / "src", "commands/done.py", "def cli():\n    raise ValueError('typed now')\n")
    _patch_paths(monkeypatch, tmp_path / "src", baseline)

    exit_code = check_exit_sites.main(["--write-baseline"])

    assert exit_code == 0
    assert json.loads(baseline.read_text(encoding="utf-8"))["files"] == {"commands/demo.py": {"helper": 1}}


def test_repository_exit_sites_match_the_committed_baseline() -> None:
    """No function gains an exit site and every removal lowers the committed baseline."""
    sites = check_exit_sites.collect(check_exit_sites.SOURCE_ROOT)

    problems = check_exit_sites.violations(sites, check_exit_sites.load_baseline(check_exit_sites.BASELINE_PATH))

    assert problems == []
