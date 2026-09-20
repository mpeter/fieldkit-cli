"""Contract tests for structured driver completion checks."""

from dataclasses import FrozenInstanceError

import pytest

from fieldkit.driver.done_checks import ArgvCheck, CheckerCheck, DoneCheckError, parse_done_checks

pytestmark = pytest.mark.unit


def _document(checks: str, *, version: str = "1", contract_extra: str = "") -> str:
    return f"""---
issues: [\"#1242\"]
done_checks:
  version: {version}
  checks:
{checks}{contract_extra}
---

# Work order
"""


def test_parses_argv_and_checker_records_with_exact_expectations() -> None:
    contract = parse_done_checks(
        _document(
            """    - id: focused-tests
      argv: [uv, run, pytest, tests/test_example.py, -q]
    - id: artifact-check
      checker: scripts/done_checks/check_example.py
      args: [\"{artifacts}/results.xml\", src/fieldkit/example.py]
      expected_exit: 3
      expected_stdout: \"ok\\n\"
"""
        )
    )

    assert contract.version == 1
    assert contract.checks == (
        ArgvCheck(
            "focused-tests",
            ("uv", "run", "pytest", "tests/test_example.py", "-q"),
            ("pytest", "tests/test_example.py", "-q"),
        ),
        CheckerCheck(
            "artifact-check",
            "scripts/done_checks/check_example.py",
            ("{artifacts}/results.xml", "src/fieldkit/example.py"),
            expected_exit=3,
            expected_stdout=b"ok\n",
        ),
    )


def test_records_are_frozen() -> None:
    check = parse_done_checks(_document("    - id: tests\n      argv: [pytest, -q]\n")).checks[0]
    with pytest.raises(FrozenInstanceError, match="cannot assign"):
        check.id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("argv", "normalized"),
    [
        ("[pytest, -q]", ("pytest", "-q")),
        ("[uv, run, ruff, check, src/]", ("ruff", "check", "src/")),
        ("[uvx, pyright, src/]", ("pyright", "src/")),
        ("[make, quality]", ("make", "quality")),
        ("[make, quality-full]", ("make", "quality-full")),
        ("[make, gazepy]", ("make", "gazepy")),
        ("[python, scripts/check.py]", ("python", "scripts/check.py")),
    ],
)
def test_accepts_and_normalizes_approved_execution_forms(argv: str, normalized: tuple[str, ...]) -> None:
    check = parse_done_checks(_document(f"    - id: check\n      argv: {argv}\n")).checks[0]
    assert isinstance(check, ArgvCheck)
    assert check.normalized_argv == normalized


@pytest.mark.parametrize(
    "argv",
    [
        "[bash, -c, pytest]",
        "[env, pytest]",
        "[xargs, pytest]",
        '[find, ., -exec, pytest, "{}", ";"]',
        "[python, -c, pass]",
        "[python, -e, pass]",
        "[python, -m, pytest]",
        "[python, -]",
        "[uv, pip, install, thing]",
        "[uv, run, uvx, pytest]",
        "[uvx, uv, run, pytest]",
        "[uvx, --from, package, pytest]",
        "[make, -f, other.mk, quality]",
        "[make, quality, gazepy]",
        "[make, NAME=value, quality]",
        "[make, unknown]",
        "[./pytest, -q]",
        "[unknown, argument]",
    ],
)
def test_rejects_forbidden_wrappers_and_executables(argv: str) -> None:
    with pytest.raises(DoneCheckError):
        parse_done_checks(_document(f"    - id: check\n      argv: {argv}\n"))


@pytest.mark.parametrize("token", ["'&&'", "'|'", "';'", "'2>file'", "'$(command)'", "'`command`'"])
def test_rejects_shell_composition_tokens(token: str) -> None:
    with pytest.raises(DoneCheckError, match="shell"):
        parse_done_checks(_document(f"    - id: check\n      argv: [grep, pattern, file, {token}]\n"))


def test_allows_regex_characters_as_literal_grep_arguments() -> None:
    check = parse_done_checks(
        _document('    - id: grep\n      argv: [grep, "^(one|two){1,2}\\\\s+\\\\$value$", src/example.py]\n')
    ).checks[0]
    assert isinstance(check, ArgvCheck)
    assert check.argv[1] == r"^(one|two){1,2}\s+\$value$"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "Uppercase"),
        ("id", "-leading"),
        ("id", "a" * 65),
        ("expected_exit", "true"),
        ("expected_exit", "-1"),
        ("expected_exit", "126"),
        ("expected_stdout", "[not-a-string]"),
    ],
)
def test_rejects_invalid_scalar_fields(field: str, value: str) -> None:
    record = "    - id: valid\n      argv: [pytest]\n"
    if field == "id":
        record = record.replace("valid", value)
    else:
        record += f"      {field}: {value}\n"
    with pytest.raises(DoneCheckError):
        parse_done_checks(_document(record))


@pytest.mark.parametrize(
    "record",
    [
        "    - id: check\n      argv: [pytest]\n      timeout: 10\n",
        "    - id: check\n      argv: [pytest]\n      profile: fast\n",
        "    - id: check\n      argv: [pytest]\n      output_limit: 10\n",
        "    - id: check\n      argv: [pytest]\n      checker: scripts/done_checks/check.py\n",
        "    - id: check\n",
        "    - id: check\n      argv: [pytest]\n      args: [file]\n",
    ],
)
def test_rejects_unknown_and_ambiguous_record_fields(record: str) -> None:
    with pytest.raises(DoneCheckError):
        parse_done_checks(_document(record))


def test_rejects_duplicate_ids() -> None:
    with pytest.raises(DoneCheckError, match="unique"):
        parse_done_checks(
            _document("    - id: same\n      argv: [pytest]\n    - id: same\n      argv: [ruff, check]\n")
        )


def test_rejects_duplicate_yaml_keys() -> None:
    with pytest.raises(DoneCheckError, match="duplicate YAML key"):
        parse_done_checks(_document("    - id: check\n      argv: [pytest]\n      argv: [ruff]\n"))


@pytest.mark.parametrize("version", ["2", "true", '"1"', "1.0"])
def test_rejects_unsupported_or_mistyped_version(version: str) -> None:
    with pytest.raises(DoneCheckError, match="version"):
        parse_done_checks(_document("    - id: check\n      argv: [pytest]\n", version=version))


@pytest.mark.parametrize("checks", ["", "    []\n"])
def test_rejects_missing_or_empty_checks(checks: str) -> None:
    with pytest.raises(DoneCheckError, match="1-64"):
        parse_done_checks(_document(checks))


def test_rejects_more_than_64_checks() -> None:
    records = "".join(f"    - id: check-{index}\n      argv: [pytest]\n" for index in range(65))
    with pytest.raises(DoneCheckError, match="1-64"):
        parse_done_checks(_document(records))


@pytest.mark.parametrize(
    "args",
    [
        "[../secret]",
        "[src/../secret]",
        "[/absolute/path]",
        '["{other}/result.xml"]',
        '["{artifacts}/../result.xml"]',
    ],
)
def test_rejects_checker_argument_escape(args: str) -> None:
    with pytest.raises(DoneCheckError):
        parse_done_checks(
            _document(f"    - id: check\n      checker: scripts/done_checks/check.py\n      args: {args}\n")
        )


@pytest.mark.parametrize(
    "checker",
    [
        "../check.py",
        "/tmp/check.py",
        "scripts/check.py",
        "scripts/done_checks/nested/check.py",
        "scripts/done_checks/check.txt",
    ],
)
def test_rejects_checker_outside_trusted_root(checker: str) -> None:
    with pytest.raises(DoneCheckError, match="scripts/done_checks"):
        parse_done_checks(_document(f"    - id: check\n      checker: {checker}\n"))


def test_rejects_item_and_stdout_size_limits() -> None:
    too_long = "x" * 4097
    with pytest.raises(DoneCheckError, match="4096"):
        parse_done_checks(_document(f"    - id: check\n      argv: [grep, {too_long}]\n"))
    huge_stdout = "x" * (64 * 1024 + 1)
    with pytest.raises(DoneCheckError, match="64 KiB"):
        parse_done_checks(_document(f"    - id: check\n      argv: [pytest]\n      expected_stdout: {huge_stdout}\n"))


def test_rejects_more_than_128_arguments_and_combined_check_size() -> None:
    too_many = ", ".join(["x"] * 129)
    with pytest.raises(DoneCheckError, match="1-128"):
        parse_done_checks(_document(f"    - id: check\n      argv: [grep, {too_many}]\n"))
    large_args = ", ".join(["x" * 1024] * 65)
    with pytest.raises(DoneCheckError, match="64 KiB"):
        parse_done_checks(
            _document(f"    - id: check\n      checker: scripts/done_checks/check.py\n      args: [{large_args}]\n")
        )


@pytest.mark.parametrize("text", ["# no frontmatter", "---\nissues: [1]\n---\n", "---\ndone_checks: [\n---\n"])
def test_rejects_missing_or_malformed_frontmatter(text: str) -> None:
    with pytest.raises(DoneCheckError):
        parse_done_checks(text)
