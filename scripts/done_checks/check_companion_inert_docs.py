#!/usr/bin/env python3
"""Check companion inert-mode documentation and characterization evidence."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

_OLD_LOOP_CLAIM = "additionally execute allowlisted commands (via the runner gate)."
_OLD_CLI_CLAIM = "additionally runs allowlisted commands."
_TESTS = (
    "test_act_runs_allowlisted_mutation",
    "test_act_denies_non_allowlisted_mutation",
)


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        print(f"cannot read {path}: {exc}")
        return None


def _junit_counts(path: str) -> tuple[int, int, int, int] | None:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        print(f"cannot parse JUnit XML {path}: {exc}")
        return None
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        print(f"JUnit XML {path} contains no testsuite")
        return None
    top = suites[0] if root.tag == "testsuites" else root
    try:
        return tuple(int(top.attrib.get(name, "0")) for name in ("tests", "failures", "errors", "skipped"))  # type: ignore[return-value]
    except ValueError:
        print(f"JUnit XML {path} has non-integer counts")
        return None


def main(argv: list[str]) -> int:
    if len(argv) != 8:
        print("usage: check_companion_inert_docs.py JUNIT DECIDE LOOP CLI SERVICE TESTS SUCCESSION CHANGELOG")
        return 1
    junit, *document_paths = argv
    documents = [_read(path) for path in document_paths]
    counts = _junit_counts(junit)
    if counts is None or any(text is None for text in documents):
        return 1
    decide, loop, cli, service, tests, succession, changelog = documents
    assert all(text is not None for text in documents)

    failures: list[str] = []
    if counts != (2, 0, 0, 0):
        failures.append(f"expected JUnit counts (2, 0, 0, 0), found {counts}")
    assert decide is not None
    if "every tier without consulting the act allowlist" not in decide:
        failures.append("decide command_argv contract still misstates read-only allowlist behavior")
    for label, text in (("loop", loop), ("CLI", cli), ("service", service)):
        assert text is not None
        if "historic regression" not in text or "structurally inert" not in text:
            failures.append(f"{label} lacks the historic regression structurally inert statement")
    assert loop is not None and cli is not None and tests is not None
    if _OLD_LOOP_CLAIM in loop:
        failures.append("obsolete loop execution claim remains")
    if _OLD_CLI_CLAIM in cli:
        failures.append("obsolete CLI execution claim remains")
    lines = tests.splitlines()
    if sum(line.strip() == "@pytest.mark.characterization" for line in lines) != 2:
        failures.append("expected exactly two characterization decorators")
    for test_name in _TESTS:
        definition = f"def {test_name}"
        indices = [index for index, line in enumerate(lines) if line.startswith(definition)]
        if len(indices) != 1 or indices[0] == 0 or lines[indices[0] - 1].strip() != "@pytest.mark.characterization":
            failures.append(f"{test_name} is not directly marked characterization")
    assert succession is not None and changelog is not None
    if "historic regression" not in succession or "structurally\n" not in succession:
        failures.append("succession plan lacks the historic regression structural block")
    if "historic regression" not in changelog:
        failures.append("changelog fragment lacks historic regression")
    if failures:
        for failure in failures:
            print(f"companion inert docs: {failure}")
        return 1
    print("companion inert docs: valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
