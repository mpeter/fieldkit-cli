"""Parse and validate trusted work-order completion-check contracts.

This module only describes execution plans.  It never resolves or launches a
process; the independent verifier owns those trust-boundary operations.
"""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TypeAlias

import yaml

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---(?:[ \t]*\r?\n|\Z)", re.DOTALL)
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_ARTIFACT_RE = re.compile(r"^\{artifacts\}/(.+)$")
_PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_-]*\}")
_SHELL_TOKENS = frozenset({"&", "&&", "|", "||", ";", "<", ">", ">>", "<<"})
_FORBIDDEN_EXECUTABLES = frozenset({"ash", "bash", "csh", "dash", "env", "fish", "find", "ksh", "sh", "xargs", "zsh"})
_LEAF_EXECUTABLES = frozenset({"grep", "test", "python", "pytest", "ruff", "mypy", "pyright", "tach"})
_MAKE_TARGETS = frozenset({"quality", "quality-full", "gazepy"})
_CHECK_KEYS = frozenset({"id", "argv", "checker", "args", "expected_exit", "expected_stdout"})
_MAX_ITEMS = 128
_MAX_ITEM_BYTES = 4096
_MAX_CHECK_BYTES = 64 * 1024
_MAX_STDOUT_BYTES = 64 * 1024


class DoneCheckError(ValueError):
    """A work order has an invalid structured completion-check contract."""


@dataclass(frozen=True)
class ArgvCheck:
    """A validated command and its shell-free direct-execution plan."""

    id: str
    argv: tuple[str, ...]
    normalized_argv: tuple[str, ...]
    expected_exit: int = 0
    expected_stdout: bytes | None = None


@dataclass(frozen=True)
class CheckerCheck:
    """A validated reference to an isolated trusted checker."""

    id: str
    checker: str
    args: tuple[str, ...]
    expected_exit: int = 0
    expected_stdout: bytes | None = None


DoneCheck: TypeAlias = ArgvCheck | CheckerCheck


@dataclass(frozen=True)
class DoneCheckContract:
    """The sole versioned completion-check authority from frontmatter."""

    version: int
    checks: tuple[DoneCheck, ...]


class _UniqueKeyLoader(yaml.SafeLoader):  # type: ignore[misc]
    """Safe YAML loader which fails rather than silently replacing a key."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise DoneCheckError("mapping keys must be scalar values") from exc
        if duplicate:
            raise DoneCheckError(f"duplicate YAML key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _mapping(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise DoneCheckError(f"{location} must be an object")
    return value


def _strings(value: object, location: str, *, allow_empty_list: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty_list) or len(value) > _MAX_ITEMS:
        qualifier = "0-128" if allow_empty_list else "1-128"
        raise DoneCheckError(f"{location} must contain {qualifier} strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item.encode("utf-8")) > _MAX_ITEM_BYTES or "\x00" in item:
            raise DoneCheckError(f"{location} entries must be nonempty strings of at most 4096 UTF-8 bytes")
        _validate_argument(item, location)
        result.append(item)
    return tuple(result)


def _has_traversal(value: str) -> bool:
    path = value.replace("\\", "/")
    return ".." in PurePosixPath(path).parts


def _validate_argument(value: str, location: str) -> None:
    if "\n" in value or "\r" in value or value in _SHELL_TOKENS or re.match(r"^\d*(?:>|<)", value):
        raise DoneCheckError(f"{location} contains shell composition syntax: {value!r}")
    if "`" in value or "$(" in value or "${" in value:
        raise DoneCheckError(f"{location} contains shell evaluation syntax: {value!r}")
    if _has_traversal(value):
        raise DoneCheckError(f"{location} contains path traversal: {value!r}")
    artifact = _ARTIFACT_RE.fullmatch(value)
    if artifact is not None:
        if not _safe_relative_path(artifact.group(1)):
            raise DoneCheckError(f"{location} contains an unsupported placeholder: {value!r}")
    elif _PLACEHOLDER_RE.search(value):
        raise DoneCheckError(f"{location} contains an unsupported placeholder: {value!r}")


def _safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value.replace("\\", "/"))
    return bool(value) and not path.is_absolute() and ".." not in path.parts


def _unwrap_leaf(argv: tuple[str, ...]) -> tuple[str, ...]:
    executable = argv[0]
    if executable == "uv":
        if len(argv) < 3 or argv[1] != "run":
            raise DoneCheckError("uv is allowed only as 'uv run <tool> ...'")
        return argv[2:]
    if executable == "uvx":
        if len(argv) < 2 or argv[1].startswith("-"):
            raise DoneCheckError("uvx requires a leaf tool without wrapper options")
        return argv[1:]
    return argv


def _validate_python_leaf(leaf: tuple[str, ...]) -> None:
    if len(leaf) < 2 or leaf[1] in {"-", "-c", "-e", "-m"} or leaf[1].startswith("-"):
        raise DoneCheckError("python requires a candidate-relative script path")
    if not leaf[1].endswith(".py") or not _safe_relative_path(leaf[1]):
        raise DoneCheckError("python script must be a safe candidate-relative .py path")


def _normalize_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    executable = argv[0]
    if "/" in executable or executable in _FORBIDDEN_EXECUTABLES:
        raise DoneCheckError(f"untrusted executable: {executable!r}")
    if executable == "make":
        if len(argv) != 2 or argv[1] not in _MAKE_TARGETS:
            raise DoneCheckError("make requires exactly one approved target: quality, quality-full, or gazepy")
        return argv

    leaf = _unwrap_leaf(argv)
    leaf_executable = leaf[0]
    if leaf_executable in {"uv", "uvx", "make"} or leaf_executable not in _LEAF_EXECUTABLES:
        raise DoneCheckError(f"untrusted leaf executable: {leaf_executable!r}")
    if leaf_executable == "python":
        _validate_python_leaf(leaf)
    return leaf


def _expectation(record: dict[str, object], location: str) -> tuple[int, bytes | None]:
    expected_exit = record.get("expected_exit", 0)
    if isinstance(expected_exit, bool) or not isinstance(expected_exit, int) or not 0 <= expected_exit <= 125:
        raise DoneCheckError(f"{location}.expected_exit must be an integer from 0 through 125")
    stdout = record.get("expected_stdout")
    if stdout is not None and not isinstance(stdout, str):
        raise DoneCheckError(f"{location}.expected_stdout must be a string")
    stdout_bytes = stdout.encode("utf-8") if isinstance(stdout, str) else None
    if stdout_bytes is not None and len(stdout_bytes) > _MAX_STDOUT_BYTES:
        raise DoneCheckError(f"{location}.expected_stdout exceeds 64 KiB")
    return expected_exit, stdout_bytes


def _parse_argv_check(
    record: dict[str, object], location: str, check_id: str, expected_exit: int, expected_stdout: bytes | None
) -> ArgvCheck:
    if "args" in record:
        raise DoneCheckError(f"{location}.args is valid only with checker")
    argv = _strings(record["argv"], f"{location}.argv")
    normalized = _normalize_argv(argv)
    if sum(len(item.encode("utf-8")) for item in argv) > _MAX_CHECK_BYTES:
        raise DoneCheckError(f"{location} exceeds 64 KiB")
    return ArgvCheck(check_id, argv, normalized, expected_exit, expected_stdout)


def _parse_checker_check(
    record: dict[str, object], location: str, check_id: str, expected_exit: int, expected_stdout: bytes | None
) -> CheckerCheck:
    checker = record["checker"]
    if not isinstance(checker, str) or "\x00" in checker or len(checker.encode("utf-8")) > _MAX_ITEM_BYTES:
        raise DoneCheckError(f"{location}.checker must be a nonempty string of at most 4096 UTF-8 bytes")
    checker_path = PurePosixPath(checker.replace("\\", "/"))
    if (
        not _safe_relative_path(checker)
        or checker_path.parent != PurePosixPath("scripts/done_checks")
        or checker_path.suffix != ".py"
    ):
        raise DoneCheckError(f"{location}.checker must name a .py file directly under scripts/done_checks")
    args = _strings(record.get("args", []), f"{location}.args", allow_empty_list=True)
    if any(PurePosixPath(arg.replace("\\", "/")).is_absolute() for arg in args):
        raise DoneCheckError(f"{location}.args must use candidate-relative or reserved artifact paths")
    strings_size = len(checker.encode("utf-8")) + sum(len(item.encode("utf-8")) for item in args)
    if strings_size > _MAX_CHECK_BYTES:
        raise DoneCheckError(f"{location} exceeds 64 KiB")
    return CheckerCheck(check_id, checker, args, expected_exit, expected_stdout)


def _parse_check(value: object, index: int) -> DoneCheck:
    location = f"done_checks.checks[{index}]"
    record = _mapping(value, location)
    unknown = set(record) - _CHECK_KEYS
    if unknown:
        raise DoneCheckError(f"{location} has unknown keys: {', '.join(sorted(unknown))}")
    check_id = record.get("id")
    if not isinstance(check_id, str) or _ID_RE.fullmatch(check_id) is None:
        raise DoneCheckError(f"{location}.id must match {_ID_RE.pattern}")
    has_argv = "argv" in record
    if has_argv == ("checker" in record):
        raise DoneCheckError(f"{location} must contain exactly one of argv or checker")
    expected_exit, expected_stdout = _expectation(record, location)
    if has_argv:
        return _parse_argv_check(record, location, check_id, expected_exit, expected_stdout)
    return _parse_checker_check(record, location, check_id, expected_exit, expected_stdout)


def parse_done_checks(text: str) -> DoneCheckContract:
    """Parse the sole version-1 ``done_checks`` authority from Markdown frontmatter."""
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise DoneCheckError("work order must begin with YAML frontmatter")
    try:
        loaded = yaml.load(match.group(1), Loader=_UniqueKeyLoader)
    except DoneCheckError:
        raise
    except yaml.YAMLError as exc:
        raise DoneCheckError(f"invalid YAML frontmatter: {exc}") from exc
    frontmatter = _mapping(loaded, "frontmatter")
    if "done_checks" not in frontmatter:
        raise DoneCheckError("frontmatter must contain done_checks")
    contract = _mapping(frontmatter["done_checks"], "done_checks")
    unknown = set(contract) - {"version", "checks"}
    if unknown:
        raise DoneCheckError(f"done_checks has unknown keys: {', '.join(sorted(unknown))}")
    version = contract.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise DoneCheckError("done_checks.version must be integer 1")
    raw_checks = contract.get("checks")
    if not isinstance(raw_checks, list) or not 1 <= len(raw_checks) <= 64:
        raise DoneCheckError("done_checks.checks must contain 1-64 records")
    checks = tuple(_parse_check(value, index) for index, value in enumerate(raw_checks))
    ids = [check.id for check in checks]
    if len(ids) != len(set(ids)):
        raise DoneCheckError("done_checks check IDs must be unique")
    return DoneCheckContract(version=1, checks=checks)
