"""Choose auditable pytest argv; the calling quality stage owns the time limit."""

import argparse
import json
import os
import sys
from pathlib import Path

from git_worktree import head_revision, require_clean_worktree
from semantic_python_changes import requires_full_test_suite

_FULL_SUITE_WORKERS = 4


def main() -> None:
    """Replace this process with pytest, preserving the supervisor's process group."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head")
    parser.add_argument("--junitxml")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--selection-report", type=Path)
    args = parser.parse_args()
    try:
        revision = head_revision(Path.cwd())
        if args.head is not None:
            require_clean_worktree(Path.cwd())
            if revision != args.head:
                parser.error("checkout must match the requested candidate revision")
    except ValueError as error:
        parser.error(str(error))
    full_suite = requires_full_test_suite(args.base, Path.cwd(), candidate_revision=args.head)
    command = [sys.executable, "-m", "pytest", "tests/", "-q", "-o", "addopts=", "--strict-markers", "--strict-config"]
    if full_suite:
        command.extend(["-p", "no:tach", "-n", str(_FULL_SUITE_WORKERS)])
    else:
        command.extend(["--tach", "--tach-base", args.base, "-n", "0"])
        if args.head is not None:
            command.extend(["--tach-head", args.head])
    if args.junitxml is not None:
        command.append(f"--junitxml={args.junitxml}")
    scope = "full-repository" if full_suite else "tach-selected"
    record = {"schema_version": 1, "source_revision": revision, "test_scope": scope, "argv": command}
    if args.selection_report is not None:
        args.selection_report.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.selection_report.with_suffix(".tmp")
        temporary.write_text(json.dumps(record) + "\n", encoding="utf-8")
        temporary.replace(args.selection_report)
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as stream:
            stream.write(f"test_scope={scope}\n")
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()
    os.environ.pop("PYTEST_ADDOPTS", None)
    os.execv(sys.executable, command)


if __name__ == "__main__":
    main()
