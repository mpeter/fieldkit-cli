"""fieldkit.companion.gate — tier-based permission checks (design D4).

Tiers are a declared contract checked here and honored by the agent per
the bundled skill — not an OS sandbox (recorded honestly in the design).
``read`` allows only the static read-only table; ``propose`` adds
outbox writes (file convention, not a command); ``act`` additionally
allows exact-argv allowlist entries.
"""

from typing import Literal

from fieldkit.companion.mapping import READ_ONLY_COMMANDS

Tier = Literal["read", "propose", "act"]
"""The companion permission tiers, in ascending order of capability."""

# The single home for the tier vocabulary AND its ordering (read ⊂ propose ⊂ act).
# Consumers that need to compare tiers (e.g. the loop's --tier lowering guard) derive
# their rank from this tuple's index rather than defining a second ordered copy.
TIER_ORDER: tuple[Tier, ...] = ("read", "propose", "act")

_TIERS = TIER_ORDER


def _is_read_only(argv: list[str]) -> bool:
    """True when *argv* names a command in the static read-only table."""
    if not argv:
        return False
    group = argv[0]
    sub = next((tok for tok in argv[1:] if not tok.startswith("-")), None)
    return (group, sub) in READ_ONLY_COMMANDS or (group, None) in READ_ONLY_COMMANDS


def _matches_allowlist_entry(argv: list[str], entry: str) -> bool:
    """Exact-argv token matching (design D4) — never prefix matching.

    The invocation must (a) start with the entry's non-flag command
    tokens in order, and (b) include every flag token the entry names.
    So an entry ``"pursuit advance --dry-run"`` does NOT permit
    ``pursuit advance`` (required ``--dry-run`` missing), and an entry
    ``"sf set-next-steps"`` does not permit ``sf set-field``.
    """
    entry_tokens = entry.split()
    entry_cmd = [t for t in entry_tokens if not t.startswith("-")]
    entry_flags = {t for t in entry_tokens if t.startswith("-")}
    if argv[: len(entry_cmd)] != entry_cmd:
        return False
    return entry_flags.issubset(set(argv))


def is_allowed(argv: list[str], tier: str, allowlist: list[str]) -> bool:
    """Return True when *argv* is permitted at *tier*.

    Args:
        argv: The fieldkit command tokens (without the program name),
            e.g. ``["pursuit", "advance", "acme/deal", "--dry-run"]``.
        tier: One of ``read``, ``propose``, ``act``. Unknown tiers deny
            everything (misconfiguration must fail closed).
        allowlist: Exact-argv entries, consulted only at ``act`` tier.
    """
    if tier not in _TIERS or not argv:
        return False
    if _is_read_only(argv):
        return True
    if tier != "act":
        # propose adds outbox FILE writes, not commands — nothing extra here.
        return False
    return any(_matches_allowlist_entry(argv, entry) for entry in allowlist if entry.strip())


def validate_allowlist(allowlist: list[str], dry_run_capable: set[str]) -> list[str]:
    """Return problems with *allowlist* entries (empty list = valid).

    Each entry must reference a command that supports ``--dry-run``
    (design D4 — act-tier commands must be previewable). The caller
    supplies *dry_run_capable* as ``"group subcommand"`` strings from
    the Click parameter registry.
    """
    problems: list[str] = []
    for entry in allowlist:
        tokens = [t for t in entry.split() if not t.startswith("-")]
        if len(tokens) < 2:
            problems.append(f"allowlist entry {entry!r}: must name a group and subcommand")
            continue
        cmd = f"{tokens[0]} {tokens[1]}"
        if cmd not in dry_run_capable:
            problems.append(f"allowlist entry {entry!r}: '{cmd}' does not support --dry-run")
    return problems
