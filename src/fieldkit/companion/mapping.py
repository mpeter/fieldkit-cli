"""fieldkit.companion.mapping — static severity, skill, and read-only tables.

These tables ARE the documented operational judgment (design D3) — the
same philosophy as ``pursuit/stage_weights.py``. They are string
contracts, so each is pinned by a characterization test that diffs it
against its upstream source (``watch/constants.py:KNOWN_WATCHERS``,
``__main__.py:_COMMANDS``) — the Pre-Mortem 4 lesson applied on day one.
"""

from dataclasses import dataclass
from typing import Final, Literal

# ---------------------------------------------------------------------------
# Watcher source → severity floor.
# Keys MUST cover watch/constants.py:KNOWN_WATCHERS (test_companion_mapping).
# Severity vocabulary reuses the watcher outcome idea: how urgently should
# an agent look at items from this source.
# ---------------------------------------------------------------------------

WATCHER_SEVERITY_MAP: Final[dict[str, str]] = {
    "morning-brief": "info",
    "pursuit-stalls": "warning",
    "account-health": "warning",
    "contract-expiry": "critical",
    "close-date-countdown": "critical",
    "slack-threads": "warning",
    "backstory-health": "warning",
    "waiting-on-tracker": "warning",
    "draft-queue": "info",
    "run-all": "info",
}

# ---------------------------------------------------------------------------
# Alert-file stem (the *-alerts.md prefix) → suggested skill.
# suggested_skill is advice for the agent, not routing (D3).
# ---------------------------------------------------------------------------

ALERT_SKILL_MAP: Final[dict[str, str]] = {
    "pursuit-stall": "grill",
    "account-health": "account-pulse",
    "contract-expiry": "engagement-health",
    "close-date-countdown": "grill",
    "slack-thread": "account-pulse",
    "backstory-health": "account-pulse",
    "waiting-on": "task-management",
    "draft-queue": "followup-draft",
}

# ---------------------------------------------------------------------------
# read-tier command grammar. This is deliberately independent of Click's
# registry: adding a CLI option must not silently grant it companion permission.
# Tests only use the registry to prove that every reviewed or denied option still
# exists with the expected arity.
# ---------------------------------------------------------------------------

ValueKind = Literal["text", "slug", "integer", "number", "date", "choice", "issue"]


@dataclass(frozen=True)
class ReadValuePolicy:
    """One bounded value accepted by the read-tier argv parser."""

    kind: ValueKind
    required: bool = True
    max_length: int = 512
    minimum: float | None = None
    maximum: float | None = None
    choices: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ReadOptionPolicy:
    """One reviewed option spelling and its value contract."""

    canonical: str
    value: ReadValuePolicy | None = None

    @property
    def arity(self) -> int:
        return 0 if self.value is None else 1


@dataclass(frozen=True)
class ReadCommandPolicy:
    """Complete static grammar for one genuine Click leaf command."""

    options: dict[str, ReadOptionPolicy]
    positionals: tuple[ReadValuePolicy, ...] = ()
    required_options: frozenset[str] = frozenset()
    denied_options: frozenset[str] = frozenset()
    required_any: tuple[frozenset[str], ...] = ()
    mutually_exclusive: tuple[frozenset[str], ...] = ()
    checked_remainder: bool = False


_FLAG = None
_TEXT = ReadValuePolicy("text")
_OPTIONAL_TEXT = ReadValuePolicy("text", required=False)
_SLUG = ReadValuePolicy("slug", max_length=128)
_OPTIONAL_SLUG = ReadValuePolicy("slug", required=False, max_length=128)
_QUERY = ReadValuePolicy("text", max_length=1024)
_DATE = ReadValuePolicy("date", max_length=10)
_LIMIT = ReadValuePolicy("integer", minimum=1, maximum=10_000, max_length=5)
_NONNEGATIVE = ReadValuePolicy("integer", minimum=0, maximum=36_500, max_length=5)
_POSITIVE_NUMBER = ReadValuePolicy("number", minimum=0, maximum=1_000_000_000_000, max_length=32)


def _option(canonical: str, value: ReadValuePolicy | None = _FLAG) -> ReadOptionPolicy:
    return ReadOptionPolicy(canonical=canonical, value=value)


def _aliases(*policies: tuple[tuple[str, ...], ReadOptionPolicy]) -> dict[str, ReadOptionPolicy]:
    return {name: policy for names, policy in policies for name in names}


def _shared_query_options(*extra: tuple[tuple[str, ...], ReadOptionPolicy]) -> dict[str, ReadOptionPolicy]:
    return _aliases(
        (("--since",), _option("--since", _DATE)),
        (("--before",), _option("--before", _DATE)),
        (("--limit",), _option("--limit", _LIMIT)),
        (("--json",), _option("--json")),
        *extra,
    )


_DB_DENIED = frozenset({"--db"})
_HELP_DENIED = frozenset({"-h", "--help"})

READ_ONLY_POLICIES: Final[dict[tuple[str, ...], ReadCommandPolicy]] = {
    ("brief", "open"): ReadCommandPolicy(
        options=_aliases((("--no-open",), _option("--no-open")), (("--json",), _option("--json"))),
        required_options=frozenset({"--no-open"}),
    ),
    ("companion", "feed"): ReadCommandPolicy(
        options=_aliases(
            (("--all",), _option("--all")),
            (("--json",), _option("--json")),
            (("--markdown",), _option("--markdown")),
            (("--account",), _option("--account", _SLUG)),
        ),
        required_options=frozenset({"--all"}),
    ),
    ("companion", "allowed"): ReadCommandPolicy(
        options=_aliases((("--json",), _option("--json"))),
        checked_remainder=True,
    ),
    ("contact", "find"): ReadCommandPolicy(
        options=_aliases(
            (("--affiliations",), _option("--affiliations")),
            (("--json",), _option("--json")),
        ),
        positionals=(_QUERY,),
        denied_options=_DB_DENIED | _HELP_DENIED,
    ),
    ("contact", "list"): ReadCommandPolicy(
        options=_aliases(
            (("--account",), _option("--account", _SLUG)),
            (("--limit",), _option("--limit", _LIMIT)),
            (("--json",), _option("--json")),
        ),
        denied_options=_HELP_DENIED,
    ),
    ("doctor", "gmail"): ReadCommandPolicy(
        options=_aliases((("--json",), _option("--json"))), denied_options=_DB_DENIED
    ),
    ("gmail", "query", "person"): ReadCommandPolicy(
        options=_shared_query_options(), positionals=(_QUERY,), denied_options=_DB_DENIED
    ),
    ("gmail", "query", "context"): ReadCommandPolicy(
        options=_shared_query_options((("--excerpt",), _option("--excerpt", _LIMIT))),
        positionals=(_QUERY,),
        denied_options=_DB_DENIED,
    ),
    ("gmail", "query", "account"): ReadCommandPolicy(
        options=_shared_query_options(), positionals=(_SLUG,), denied_options=_DB_DENIED
    ),
    ("gmail", "query", "dig"): ReadCommandPolicy(
        options=_shared_query_options(), positionals=(_SLUG, _QUERY), denied_options=_DB_DENIED
    ),
    ("gmail", "query", "threads"): ReadCommandPolicy(
        options=_shared_query_options((("--account", "-a"), _option("--account", _SLUG))),
        positionals=(_QUERY,),
        denied_options=_DB_DENIED,
    ),
    ("gmail", "query", "champion"): ReadCommandPolicy(
        options=_shared_query_options(), positionals=(_QUERY,), denied_options=_DB_DENIED
    ),
    ("gmail", "query", "blindspots"): ReadCommandPolicy(
        options=_shared_query_options(
            (("--min-messages",), _option("--min-messages", _NONNEGATIVE)),
            (("--known",), _option("--known", _QUERY)),
            (("--include-suspected",), _option("--include-suspected")),
        ),
        positionals=(_SLUG,),
        denied_options=_DB_DENIED,
    ),
    ("gmail", "decay"): ReadCommandPolicy(
        options=_aliases(
            (("--account", "-a"), _option("--account", _SLUG)),
            (("--days",), _option("--days", _NONNEGATIVE)),
            (("--all",), _option("--all")),
            (("--domain",), _option("--domain", _SLUG)),
            (("--min-messages",), _option("--min-messages", _NONNEGATIVE)),
            (("--limit",), _option("--limit", _LIMIT)),
            (("--max-age-days",), _option("--max-age-days", _NONNEGATIVE)),
            (("--json",), _option("--json")),
        ),
        required_options=frozenset({"--account"}),
        denied_options=_DB_DENIED,
    ),
    ("gmail", "backstory-gap"): ReadCommandPolicy(
        options=_aliases(
            (("--account",), _option("--account", _SLUG)),
            (("--min-messages",), _option("--min-messages", _NONNEGATIVE)),
            (("--limit",), _option("--limit", ReadValuePolicy("integer", minimum=1, maximum=500, max_length=3))),
            (("--json",), _option("--json")),
        ),
        denied_options=_DB_DENIED,
    ),
    ("ingest", "status"): ReadCommandPolicy(
        options=_aliases((("--account", "-a"), _option("--account", _SLUG)), (("--json",), _option("--json")))
    ),
    ("ingest", "backfill"): ReadCommandPolicy(
        options=_aliases(
            (("--dry-run",), _option("--dry-run")),
            (("--account", "-a"), _option("--account", _SLUG)),
            (("--json",), _option("--json")),
        ),
        required_options=frozenset({"--dry-run"}),
    ),
    ("issue", "list"): ReadCommandPolicy(
        options=_aliases(
            (
                ("--status",),
                _option(
                    "--status",
                    ReadValuePolicy(
                        "choice", choices=frozenset({"open", "planned", "fixed", "closed", "wont-fix", "all"})
                    ),
                ),
            ),
            (("--all",), _option("--all")),
            (
                ("--type",),
                _option("--type", ReadValuePolicy("choice", choices=frozenset({"bug", "enhancement", "all"}))),
            ),
            (("--module",), _option("--module", _SLUG)),
            (("--json",), _option("--json")),
        )
    ),
    ("issue", "show"): ReadCommandPolicy(
        options=_aliases((("--json",), _option("--json"))), positionals=(ReadValuePolicy("issue", max_length=32),)
    ),
    ("issue", "board"): ReadCommandPolicy(options=_aliases((("--json",), _option("--json")))),
    ("meeting", "list"): ReadCommandPolicy(
        options=_aliases((("--account", "-a"), _option("--account", _SLUG)), (("--json",), _option("--json"))),
        denied_options=_HELP_DENIED,
    ),
    ("pipeline", "quota"): ReadCommandPolicy(
        options=_aliases(
            (("--json",), _option("--json")),
            (("--account", "-a"), _option("--account", _SLUG)),
            (("--source",), _option("--source", ReadValuePolicy("choice", choices=frozenset({"pursuits", "sf"})))),
        ),
        denied_options=frozenset({"--set", "--period", "--data-root"}),
    ),
    ("pipeline", "open"): ReadCommandPolicy(
        options=_aliases(
            (("--no-open",), _option("--no-open")),
            (("--json",), _option("--json")),
            (("--account", "-a"), _option("--account", _SLUG)),
        ),
        required_options=frozenset({"--no-open"}),
    ),
    ("pursuit", "health"): ReadCommandPolicy(
        options=_aliases(
            (("--account", "-a"), _option("--account", _SLUG)),
            (("--include-prospect",), _option("--include-prospect")),
            (("--json",), _option("--json")),
            (("--strict",), _option("--strict")),
            (("--compact",), _option("--compact")),
        )
    ),
    ("pursuit", "forecast"): ReadCommandPolicy(
        options=_aliases(
            (("--account", "-a"), _option("--account", _SLUG)),
            (("--quota", "-q"), _option("--quota", _POSITIVE_NUMBER)),
            (("--json",), _option("--json")),
        )
    ),
    ("pursuit", "projects"): ReadCommandPolicy(
        options=_aliases(
            (("--account", "-a"), _option("--account", _SLUG)),
            (("--json",), _option("--json")),
            (("--strict",), _option("--strict")),
        )
    ),
    ("sf", "session-check"): ReadCommandPolicy(options=_aliases((("--json",), _option("--json")))),
    ("skill", "list"): ReadCommandPolicy(
        options=_aliases(
            (("--json",), _option("--json")),
            (("--group",), _option("--group", _SLUG)),
            (("-v", "--verbose"), _option("--verbose")),
        )
    ),
    ("skill", "show"): ReadCommandPolicy(options=_aliases((("--json",), _option("--json"))), positionals=(_SLUG,)),
    ("skill", "variables"): ReadCommandPolicy(
        options=_aliases((("--json",), _option("--json"))), positionals=(_OPTIONAL_SLUG,)
    ),
    ("version",): ReadCommandPolicy(options={}, denied_options=frozenset({"--features", "-f", "--json"})),
    ("watch", "logs"): ReadCommandPolicy(
        options=_aliases(
            (("--tail", "-n"), _option("--tail", _LIMIT)),
            (("--list",), _option("--list")),
            (("--json",), _option("--json")),
        ),
        positionals=(
            ReadValuePolicy(
                "choice",
                required=False,
                max_length=64,
                choices=frozenset(WATCHER_SEVERITY_MAP),
            ),
        ),
    ),
}

# ---------------------------------------------------------------------------
# act-tier previewability: full command paths whose CLI carries a
# literal --dry-run option. The companion CLI compiles configured act entries
# against this reviewed set and the installed registry (design D4 — act-tier
# commands must be previewable). Keyed the same way as
# READ_ONLY_POLICIES but stored as space-separated full paths. Runtime
# validation checks actual registry options and executable group boundaries.
#
# sf set-next-steps is deliberately absent: it previews by default and writes
# only behind --confirm, not --dry-run. Folding that opposite polarity into
# this table is a second design decision, not a mechanical listing (implementation note).
# data-sync is absent because it is a single-token command (no subcommand) —
# the policy compiler already rejects single-token entries on its own.
# ---------------------------------------------------------------------------

DRY_RUN_CAPABLE: Final[frozenset[str]] = frozenset(
    {
        "brief generate",
        "gtask complete",
        "gtask create",
        "ingest backfill",
        "ingest discover",
        "ingest reprocess",
        "ingest route",
        "ingest run",
        "issue sync-milestone",
        "pursuit advance",
        "pursuit archive",
        "pursuit create",
        "pursuit rename",
        "pursuit repair-dates",
        "sf frontmatter",
        "sf reconcile",
        "skill install",
        "watch run backstory-health",
        "watch run close-date-countdown",
        "watch run contract-expiry",
        "watch run draft-queue",
        "watch run pursuit-stalls",
        "watch run slack-threads",
        "watch run waiting-on-tracker",
    }
)

# Options whose effects conflict with preview safety even when the command also
# receives --dry-run. Every exception belongs here rather than in an adapter.
ACT_PREVIEW_DENIED_OPTIONS: Final[dict[str, frozenset[str]]] = {
    "watch run pursuit-stalls": frozenset({"--scrub-duplicates"}),
}


def suggested_skill_for(alert_stem: str) -> str | None:
    """Return the suggested skill for an alert-file stem, or None when unmapped.

    Matches on the longest mapped prefix so ``pursuit-stall-alerts.md``
    (stem ``pursuit-stall``) and future variants resolve consistently.
    """
    for prefix in sorted(ALERT_SKILL_MAP, key=len, reverse=True):
        if alert_stem.startswith(prefix):
            return ALERT_SKILL_MAP[prefix]
    return None
