"""Deterministic PII protection for inspectable GitHub publication commands."""

import json
import re
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from fieldkit.config import get_account_names

_MAX_BODY_FILE_BYTES = 64 * 1024
_MAX_POLICY_REQUEST_BYTES = 128 * 1024
_ABS_HOME_RE = re.compile(r"/(?:home|Users)/[A-Za-z][A-Za-z0-9_.-]{1,30}/")
_EMAIL_RE = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9._%+-]{0,63})@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_SLACK_FROM_RE = re.compile(r"\bfrom:([a-z][a-z0-9_.-]{2,30})(?=\s|$|[\"'])", re.IGNORECASE)
_SHELL_METACHARACTERS = frozenset(";|&<>()`${}!")
_GH_INTERACTIVE_OPTIONS = frozenset({"--editor", "--fill", "--fill-first", "--fill-verbose", "--template", "--web"})
_PAYLOAD_OPTIONS = {
    "--title": "title",
    "-t": "title",
    "--body": "body",
    "-b": "body",
    "--body-file": "body_file",
    "-F": "body_file",
    "--note": "body",
    "--source": "source",
}
_VALUE_METADATA_OPTIONS = frozenset(
    {
        "--assignee",
        "--base",
        "--head",
        "--label",
        "--milestone",
        "--module",
        "--project",
        "--repo",
        "--reviewer",
        "--severity",
        "--type",
    }
)
_FLAG_METADATA_OPTIONS = frozenset({"--draft", "--json", "--maintainer-edit", "--no-maintainer-edit"})
_SAFE_EMAIL_LOCALS = frozenset(
    {
        "admin",
        "alice",
        "author",
        "contact",
        "email",
        "example",
        "info",
        "name",
        "noreply",
        "placeholder",
        "test",
        "user",
        "your_email",
        "your-email",
    }
)
_SAFE_EMAIL_DOMAINS = frozenset({"example.com", "example.org", "example.net", "invalid", "localhost", "test"})
_SAFE_SLACK_HANDLES = frozenset({"user", "username", "you", "your-username"})


class ViolationCategory(StrEnum):
    """Fixed categories exposed to policy adapters."""

    ACCOUNT_NAME = "account_name"
    ABSOLUTE_HOME_PATH = "absolute_home_path"
    PERSONAL_EMAIL = "personal_email"
    SLACK_HANDLE = "slack_handle"
    UNINSPECTABLE = "uninspectable"


class PayloadSource(StrEnum):
    """Inspectable source labels exposed to policy adapters."""

    COMMAND = "command"
    TITLE = "title"
    BODY = "body"
    BODY_FILE = "body_file"
    SOURCE = "source"


@dataclass(frozen=True)
class PublicationDecision:
    """A payload-safe allow or block decision."""

    allowed: bool
    category: ViolationCategory | None = None
    source: PayloadSource | None = None


def evaluate_publication_command(
    command: str,
    *,
    cwd: Path,
    account_names_getter: Callable[[], list[str]] = get_account_names,
) -> PublicationDecision:
    """Allow non-target commands and inspect the bounded GitHub publication grammar.

    This deliberately accepts no publication-mode suppressions. A supported command
    with a payload that cannot be read deterministically is blocked before execution.
    """
    try:
        tokens = _tokenize(command)
    except ValueError:
        return _block_if_target_prefix(command)

    if _is_command_builtin_target(tokens):
        return _block(PayloadSource.COMMAND)

    target = _classify_target(tokens)
    if target is None:
        return (
            _block(PayloadSource.COMMAND) if _has_unclassified_publication_target(tokens) else PublicationDecision(True)
        )

    payloads = _extract_payloads(tokens, target)
    if payloads is None:
        return _block(PayloadSource.COMMAND)

    if missing_source := _missing_required_payload_source(target, payloads):
        return _block(missing_source)

    try:
        account_names = account_names_getter()
    except Exception:  # noqa: BLE001 - a missing detector must not permit publication
        return _block(PayloadSource.COMMAND)

    return _inspect_payloads(payloads, cwd, account_names)


def _block(source: PayloadSource, category: ViolationCategory = ViolationCategory.UNINSPECTABLE) -> PublicationDecision:
    return PublicationDecision(allowed=False, category=category, source=source)


def _block_if_target_prefix(command: str) -> PublicationDecision:
    """Fail closed for malformed shell that could contain a supported publisher."""
    if re.search(r"\b(?:pr|issue)\s+[A-Za-z]+\b", command):
        return _block(PayloadSource.COMMAND)
    return PublicationDecision(allowed=True)


def _has_unclassified_publication_target(tokens: list[str]) -> bool:
    return (
        _contains_gh_pr_or_issue_scope(tokens)
        or _contains_fieldkit_issue_scope(tokens)
        or _contains_supported_command_sequence(tokens)
    )


def _missing_required_payload_source(target: tuple[str, str], payloads: dict[str, str]) -> PayloadSource | None:
    if target == ("gh", "create") and "title" not in payloads:
        return PayloadSource.TITLE
    if _requires_body(target, payloads) and "body" not in payloads and "body_file" not in payloads:
        return PayloadSource.BODY
    return None


def _inspect_payloads(payloads: dict[str, str], cwd: Path, account_names: list[str]) -> PublicationDecision:
    for source_name, value in payloads.items():
        source = PayloadSource(source_name)
        if source is PayloadSource.BODY_FILE:
            return _inspect_body_file(value, cwd, account_names)
        category = _detect_pii(value, account_names)
        if category is not None:
            return _block(source, category)
    return PublicationDecision(allowed=True)


def _tokenize(command: str) -> list[str]:
    """Tokenize the permitted literal shell subset or raise for unsafe syntax."""
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    token_started = False

    for char in command:
        if char in "\r\n":
            raise ValueError("shell composition")
        if quote is not None:
            if char == quote:
                quote = None
            elif char == "\\" or (quote == '"' and char in "`$"):
                raise ValueError("shell expansion")
            else:
                current.append(char)
            token_started = True
            continue

        if char in "'\"":
            quote = char
            token_started = True
        elif char.isspace():
            if token_started:
                tokens.append("".join(current))
                current = []
                token_started = False
        elif char in _SHELL_METACHARACTERS or char in "\\*?[":
            raise ValueError("shell composition")
        else:
            current.append(char)
            token_started = True

    if quote is not None:
        raise ValueError("malformed quoting")
    if token_started:
        tokens.append("".join(current))
    return tokens


def _classify_target(tokens: list[str]) -> tuple[str, str] | None:
    if (
        len(tokens) >= 3
        and tokens[0] == "gh"
        and tokens[1] in {"pr", "issue"}
        and tokens[2]
        in {
            "create",
            "edit",
            "comment",
        }
    ):
        return ("gh", tokens[2])
    start = 0
    if len(tokens) >= 3 and tokens[:3] == ["uv", "run", "fieldkit"]:
        start = 3
    elif tokens[:1] == ["fieldkit"]:
        start = 1
    if len(tokens) >= start + 2 and tokens[start] == "issue":
        action = tokens[start + 1]
        if action in {"close", "create", "edit", "fix", "link", "note", "plan"}:
            return ("fieldkit", action)
    return None


def _contains_gh_pr_or_issue_scope(tokens: list[str]) -> bool:
    """Fail closed for unclassified GitHub PR and issue commands, which may publish text."""
    return any(
        Path(token).name == "gh" and any(section in {"pr", "issue"} for section in tokens[index + 1 :])
        for index, token in enumerate(tokens)
    )


def _contains_fieldkit_issue_scope(tokens: list[str]) -> bool:
    """Fail closed for unclassified fieldkit issue commands, which can publish via GitHub."""
    return any(Path(token).name == "fieldkit" and "issue" in tokens[index + 1 :] for index, token in enumerate(tokens))


def _contains_supported_command_sequence(tokens: list[str]) -> bool:
    """Detect wrapped publishers so unsupported wrappers fail closed rather than bypassing policy."""
    for index, token in enumerate(tokens):
        executable = Path(token).name
        remaining = tokens[index + 1 :]
        if executable == "gh" and _contains_gh_publication_subcommand(remaining):
            return True
        if executable == "fieldkit" and _contains_fieldkit_issue_subcommand(remaining):
            return True
        if executable == "uv" and _contains_uv_fieldkit_issue_subcommand(remaining):
            return True
        if re.search(
            r"(?:^|\s)(?:(?:\S*/)?gh\b.*\b(?:pr|issue)\b|"
            r"(?:uv\s+run\s+)?fieldkit\s+issue\b)",
            token,
        ):
            return True
    return False


def _contains_gh_publication_subcommand(tokens: list[str]) -> bool:
    return any(
        tokens[index] in {"pr", "issue"} and tokens[index + 1] in {"create", "edit", "comment"}
        for index in range(len(tokens) - 1)
    )


def _contains_fieldkit_issue_subcommand(tokens: list[str]) -> bool:
    return any(
        tokens[index : index + 2] == ["issue", action]
        for index in range(len(tokens) - 1)
        for action in {"close", "create", "edit", "fix", "link", "note", "plan"}
    )


def _contains_uv_fieldkit_issue_subcommand(tokens: list[str]) -> bool:
    return any(
        tokens[index : index + 4] == ["run", "fieldkit", "issue", action]
        for index in range(len(tokens) - 3)
        for action in {"close", "create", "edit", "fix", "link", "note", "plan"}
    )


def _is_command_builtin_target(tokens: list[str]) -> bool:
    return (
        len(tokens) >= 4
        and tokens[0] == "command"
        and tokens[1] == "gh"
        and tokens[2] in {"pr", "issue"}
        and tokens[3] in {"create", "edit", "comment"}
    )


def _extract_payloads(tokens: list[str], target: tuple[str, str]) -> dict[str, str] | None:
    prefix_length = 3 if target[0] == "gh" else (5 if tokens[:3] == ["uv", "run", "fieldkit"] else 3)
    arguments = tokens[prefix_length:]
    payloads: dict[str, str] = {}
    positionals: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        consumed = _consume_payload_argument(arguments, index, payloads)
        if consumed == -1:
            return None
        if consumed is not None:
            index = consumed
        elif argument in _GH_INTERACTIVE_OPTIONS:
            return None
        elif argument in _VALUE_METADATA_OPTIONS:
            index += 1
            if index == len(arguments) or arguments[index].startswith("-"):
                return None
        elif argument in _FLAG_METADATA_OPTIONS:
            pass
        elif argument.startswith("-"):
            return None
        else:
            positionals.append(argument)
        index += 1

    if not _add_positional_payload(target, positionals, payloads):
        return None
    return payloads


def _consume_payload_argument(arguments: list[str], index: int, payloads: dict[str, str]) -> int | None:
    option, equals, value = arguments[index].partition("=")
    payload_name = _PAYLOAD_OPTIONS.get(option)
    if payload_name is None:
        return None
    if payload_name in payloads:
        return -1
    if not equals:
        index += 1
        if index == len(arguments) or arguments[index].startswith("-"):
            return -1
        value = arguments[index]
    if (equals and not value) or (payload_name == "body_file" and value == "-"):
        return -1
    payloads[payload_name] = value
    return index


def _add_positional_payload(target: tuple[str, str], positionals: list[str], payloads: dict[str, str]) -> bool:
    if target not in {("fieldkit", "note"), ("fieldkit", "link")}:
        return True
    if len(positionals) < 2:
        return False
    payloads["body" if target[1] == "note" else "title"] = positionals[-1]
    return True


def _requires_body(target: tuple[str, str], payloads: dict[str, str]) -> bool:
    if target == ("gh", "comment"):
        return True
    return target == ("gh", "create")


def _inspect_body_file(value: str, cwd: Path, account_names: list[str]) -> PublicationDecision:
    """Classify file content, then block because GitHub CLI would reopen its mutable path."""
    path = cwd / value
    try:
        file_stat = path.lstat()
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > _MAX_BODY_FILE_BYTES:
            return _block(PayloadSource.BODY_FILE)
        content = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return _block(PayloadSource.BODY_FILE)
    category = _detect_pii(content, account_names)
    if category is not None:
        return _block(PayloadSource.BODY_FILE, category)
    return _block(PayloadSource.BODY_FILE)


def _detect_pii(value: str, account_names: list[str]) -> ViolationCategory | None:
    account_pattern = _account_name_pattern(account_names)
    if account_pattern is not None and account_pattern.search(value):
        return ViolationCategory.ACCOUNT_NAME
    if _ABS_HOME_RE.search(value):
        return ViolationCategory.ABSOLUTE_HOME_PATH
    if _contains_personal_email(value):
        return ViolationCategory.PERSONAL_EMAIL
    match = _SLACK_FROM_RE.search(value)
    if match is not None and match.group(1).lower() not in _SAFE_SLACK_HANDLES:
        return ViolationCategory.SLACK_HANDLE
    return None


def _account_name_pattern(account_names: list[str]) -> re.Pattern[str] | None:
    names = [name.strip() for name in account_names if name.strip()]
    if not names:
        return None
    return re.compile(rf"(?<!\w)(?:{'|'.join(re.escape(name) for name in names)})(?!\w)", re.IGNORECASE)


def _contains_personal_email(value: str) -> bool:
    for match in _EMAIL_RE.finditer(value):
        local, domain = match.group(1).lower(), match.group(2).lower()
        if local not in _SAFE_EMAIL_LOCALS or domain not in _SAFE_EMAIL_DOMAINS:
            return True
    return False


def main() -> int:
    """Evaluate one JSON-lines policy request without exposing its command text."""
    request_line = sys.stdin.buffer.readline(_MAX_POLICY_REQUEST_BYTES + 1)
    if len(request_line) > _MAX_POLICY_REQUEST_BYTES or sys.stdin.buffer.read(1):
        return 1
    try:
        request = json.loads(request_line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 1
    if not isinstance(request, dict):
        return 1
    command = request.get("command")
    cwd = request.get("cwd")
    if not isinstance(command, str) or not isinstance(cwd, str):
        return 1

    decision = evaluate_publication_command(command, cwd=Path(cwd))
    sys.stdout.write(
        json.dumps(
            {
                "allowed": decision.allowed,
                "category": decision.category,
                "source": decision.source,
            }
        )
        + "\n"
    )
    return 0
