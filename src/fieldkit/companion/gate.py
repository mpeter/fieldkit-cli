"""fieldkit.companion.gate — tier-based permission checks (design D4).

Tiers are a declared contract checked here and honored by the agent per
the bundled skill — not an OS sandbox (recorded honestly in the design).
``read`` allows only the static read-only table; ``propose`` adds
outbox writes (file convention, not a command); ``act`` additionally
allows exact-argv allowlist entries.
"""

import math
import re
import shlex
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Literal

from fieldkit.companion.mapping import READ_ONLY_POLICIES, ReadCommandPolicy, ReadValuePolicy

Tier = Literal["read", "propose", "act"]
"""The companion permission tiers, in ascending order of capability."""

# The single home for the tier vocabulary AND its ordering (read ⊂ propose ⊂ act).
# Consumers that need to compare tiers (e.g. the loop's --tier lowering guard) derive
# their rank from this tuple's index rather than defining a second ordered copy.
TIER_ORDER: tuple[Tier, ...] = ("read", "propose", "act")

_TIERS = TIER_ORDER


_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_ISSUE_RE = re.compile(r"fieldkit-0*[1-9][0-9]*\Z", re.IGNORECASE | re.ASCII)
_MAX_ALLOWED_ARGV = 64
_MAX_ALLOWED_TOKEN = 4096
_MAX_ALLOWED_TOTAL = 16_384
_POLICY_TOKEN = object()


@dataclass(frozen=True)
class PreviewCommandPolicy:
    """Actual option arities and command boundary for one reviewed preview."""

    option_arities: dict[str, int]
    is_group: bool
    denied_options: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ValidatedActPolicy:
    """Exact command entries admitted by the canonical preview validator."""

    _entries: frozenset[tuple[str, ...]]
    _configured_entries: tuple[str, ...]
    _token: object


@dataclass(frozen=True)
class ActPolicyValidation:
    """Result of validating configured act-tier command entries."""

    policy: ValidatedActPolicy | None
    problems: tuple[str, ...]


NO_ACT_POLICY = ValidatedActPolicy(frozenset(), (), _POLICY_TOKEN)


def _safe_token(value: str) -> bool:
    return not any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in value)


def _preview_problem(tokens: list[str], policy: PreviewCommandPolicy) -> str | None:
    index = 0
    preview = False
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            if policy.is_group:
                return "must stop at the reviewed executable command boundary"
            index += 1
            continue
        if not token.startswith("--") and len(token) != 2:
            return "contains an unsupported option spelling"
        name, separator, value = token.partition("=")
        arity = policy.option_arities.get(name)
        if arity is None or (separator and (arity != 1 or not name.startswith("--") or not value)):
            return "contains an unknown or malformed option"
        if name in policy.denied_options:
            return f"contains conflicting effect option {name}"
        if not separator:
            values = tokens[index + 1 : index + 1 + arity]
            if len(values) != arity or any(not value or value.startswith("-") for value in values):
                return "contains a missing or option-looking value"
            index += arity
        if name == "--dry-run" and arity == 0:
            preview = True
        index += 1
    if not preview:
        return "must invoke the reviewed command with a parsed --dry-run flag"
    return None


def _valid_value(value: str, policy: ReadValuePolicy) -> bool:
    """Validate a single option or positional value without resolving paths."""
    if not value or value.startswith("-") or len(value) > policy.max_length or not _safe_token(value):
        return False
    if policy.kind == "text":
        return True
    if policy.kind == "slug":
        return _SLUG_RE.fullmatch(value) is not None
    if policy.kind == "choice":
        return value in policy.choices
    if policy.kind == "issue":
        return _ISSUE_RE.fullmatch(value) is not None
    if policy.kind == "date":
        try:
            return len(value) == 10 and date.fromisoformat(value).isoformat() == value
        except ValueError:
            return False
    try:
        number = int(value) if policy.kind == "integer" else float(value)
    except ValueError:
        return False
    if isinstance(number, float) and not math.isfinite(number):
        return False
    return (policy.minimum is None or number >= policy.minimum) and (policy.maximum is None or number <= policy.maximum)


def _is_checked_remainder(tokens: list[str]) -> bool:
    """Validate ``companion allowed`` without interpreting its target as execution."""
    if "--" not in tokens or tokens.count("--") != 1:
        return False
    boundary = tokens.index("--")
    prefix = tokens[:boundary]
    target = tokens[boundary + 1 :]
    if prefix not in ([], ["--json"]):
        return False
    if not target or len(target) > _MAX_ALLOWED_ARGV:
        return False
    if target[:2] == ["companion", "allowed"]:
        return False
    if sum(len(token) for token in target) > _MAX_ALLOWED_TOTAL:
        return False
    return all(token and len(token) <= _MAX_ALLOWED_TOKEN and _safe_token(token) for token in target)


def _matches_read_policy(tokens: list[str], policy: ReadCommandPolicy) -> bool:
    """Parse a complete leaf argv against one explicit read policy."""
    if policy.checked_remainder:
        return _is_checked_remainder(tokens)

    present: set[str] = set()
    positionals: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return False
        if token.startswith("-"):
            if not token.startswith("--") and len(token) != 2:
                return False
            name, separator, inline_value = token.partition("=")
            option = policy.options.get(name)
            if option is None or option.canonical in present:
                return False
            present.add(option.canonical)
            if option.value is None:
                if separator:
                    return False
            else:
                if separator:
                    value = inline_value
                else:
                    index += 1
                    if index >= len(tokens) or tokens[index].startswith("-"):
                        return False
                    value = tokens[index]
                if not _valid_value(value, option.value):
                    return False
        else:
            positionals.append(token)
        index += 1

    required_positionals = sum(value.required for value in policy.positionals)
    if not required_positionals <= len(positionals) <= len(policy.positionals):
        return False
    for position, value in enumerate(positionals):
        if not _valid_value(value, policy.positionals[position]):
            return False

    present.update(f"arg:{position}" for position in range(len(positionals)))
    if not policy.required_options <= present:
        return False
    if any(not choices & present for choices in policy.required_any):
        return False
    return not any(len(choices & present) > 1 for choices in policy.mutually_exclusive)


def _is_read_only(argv: list[str]) -> bool:
    """True when *argv* matches one complete static read-only grammar."""
    if not argv:
        return False
    for path in sorted(READ_ONLY_POLICIES, key=len, reverse=True):
        if tuple(argv[: len(path)]) == path:
            return _matches_read_policy(argv[len(path) :], READ_ONLY_POLICIES[path])
    return False


def is_allowed(argv: list[str], tier: str, policy: ValidatedActPolicy) -> bool:
    """Return True when *argv* is permitted at *tier*.

    Args:
        argv: The fieldkit command tokens (without the program name),
            e.g. ``["pursuit", "advance", "acme/deal", "--dry-run"]``.
        tier: One of ``read``, ``propose``, ``act``. Unknown tiers deny
            everything (misconfiguration must fail closed).
        policy: Canonically validated exact-argv permissions. Raw configured
            strings are never accepted at this execution boundary.
    """
    if (
        not is_valid_act_policy(policy)
        or tier not in _TIERS
        or not argv
        or not all(_safe_token(token) for token in argv)
    ):
        return False
    if _is_read_only(argv):
        return True
    if tier != "act":
        # propose adds outbox FILE writes, not commands — nothing extra here.
        return False
    return tuple(argv) in policy._entries


def _compile_act_policy(allowlist: list[str], preview_commands: dict[str, PreviewCommandPolicy]) -> ActPolicyValidation:
    """Compile configured entries against adapter-derived command metadata.

    The Click adapter is the sole caller: it derives ``preview_commands`` from
    the installed registry and the reviewed eligible-command mapping. Domain
    effect sinks separately bind the compiled source entries to current config.
    """
    problems: list[str] = []
    entries: set[tuple[str, ...]] = set()
    for entry in allowlist:
        try:
            tokens = shlex.split(entry)
        except ValueError:
            problems.append("allowlist entry has invalid quoting")
            continue
        if (
            len(tokens) > _MAX_ALLOWED_ARGV
            or sum(len(token) for token in tokens) > _MAX_ALLOWED_TOTAL
            or any(len(token) > _MAX_ALLOWED_TOKEN for token in tokens)
        ):
            problems.append("allowlist entry exceeds the bounded command size")
            continue
        if len(tokens) < 2 or any(token.startswith("-") for token in tokens[:2]):
            problems.append(f"allowlist entry {entry!r}: must name a group and subcommand")
            continue
        if not all(_safe_token(token) for token in tokens):
            problems.append("allowlist entry contains unsafe control characters")
            continue
        matches = [path for path in preview_commands if tokens[: len(path.split())] == path.split()]
        if not matches:
            problems.append(f"allowlist entry {entry!r}: command does not support --dry-run")
            continue
        command = max(matches, key=lambda path: len(path.split()))
        problem = _preview_problem(tokens[len(command.split()) :], preview_commands[command])
        if problem is not None:
            problems.append(f"allowlist entry {entry!r}: {problem}")
            continue
        entries.add(tuple(tokens))
    if problems:
        return ActPolicyValidation(None, tuple(problems))
    return ActPolicyValidation(ValidatedActPolicy(frozenset(entries), tuple(allowlist), _POLICY_TOKEN), ())


def is_valid_act_policy(policy: object) -> bool:
    """Return whether policy entries still match their compiled source text."""
    if policy is NO_ACT_POLICY:
        return True
    if not isinstance(policy, ValidatedActPolicy) or policy._token is not _POLICY_TOKEN:
        return False
    parsed: set[tuple[str, ...]] = set()
    for entry in policy._configured_entries:
        try:
            tokens = shlex.split(entry)
        except ValueError:
            return False
        if not tokens:
            return False
        parsed.add(tuple(tokens))
    return frozenset(parsed) == policy._entries


def matches_configured_authority(tier: str, policy: object) -> bool:
    """Bind an intact compiled policy to the operator's current configuration."""
    if not isinstance(policy, ValidatedActPolicy) or not is_valid_act_policy(policy) or tier not in _TIERS:
        return False
    from fieldkit.config import get_companion_act_allowlist, get_companion_tier

    configured_tier = get_companion_tier()
    if configured_tier not in _TIERS or TIER_ORDER.index(tier) > TIER_ORDER.index(configured_tier):
        return False
    expected_source = tuple(get_companion_act_allowlist()) if tier == "act" else ()
    return policy._configured_entries == expected_source
