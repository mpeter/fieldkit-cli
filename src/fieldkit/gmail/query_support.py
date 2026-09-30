"""Shared validation and work budgets for managed Gmail-cache readers."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from html import escape

from fieldkit.config import ConfigError

DEFAULT_QUERY_LIMIT = 50
MAX_QUERY_LIMIT = 500
MAX_CONFIGURED_ACCOUNTS = 500
MAX_DOMAINS_PER_ACCOUNT = 100
MIN_SCAN_ROWS = 100
MAX_SCAN_ROWS = 10_000
SCAN_ROWS_PER_RESULT = 20
SQLITE_PROGRESS_INTERVAL = 100
DEFAULT_SQLITE_STEPS_PER_ROW = 500
MAX_SQLITE_STEPS = 5_000_000

_ACCOUNT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")

NOISE_REGEX = re.compile(
    r"noreply|no-reply|notifications|donotreply|do-not-reply|bounce|mailer-daemon|postmaster|support|alerts|automated",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ConfiguredAccountScope:
    """One exact configured account and its normalized domain boundaries."""

    key: str
    domains: tuple[str, ...]
    internal_domains: tuple[str, ...]


@dataclass
class SQLiteQueryBudget:
    """Mutable progress state for one bounded SQLite query sequence."""

    maximum_steps: int
    steps: int = 0
    exhausted: bool = False

    def progress(self) -> int:
        """Interrupt SQLite after the reviewed virtual-machine step budget."""
        self.steps += SQLITE_PROGRESS_INTERVAL
        if self.steps > self.maximum_steps:
            self.exhausted = True
            return 1
        return 0


def _normalized_domains(value: object, *, field: str, required: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or (required and not value):
        raise ConfigError(f"accounts.yaml has invalid {field}")
    if len(value) > MAX_DOMAINS_PER_ACCOUNT:
        raise ConfigError(f"accounts.yaml has too many {field}")
    result: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            raise ConfigError(f"accounts.yaml has invalid {field}")
        domain = normalize_domain(raw, field=field)
        if domain not in result:
            result.append(domain)
    return tuple(result)


def normalize_domain(value: str, *, field: str = "domain") -> str:
    """Return one bounded lower-case domain or raise a fixed config error."""
    domain = value.strip().lower().rstrip(".")
    if not domain or _DOMAIN_RE.fullmatch(domain) is None or ".." in domain:
        raise ConfigError(f"accounts.yaml has invalid {field}")
    return domain


def domain_matches_boundary(domain: str, configured_domains: tuple[str, ...]) -> bool:
    """Return whether a domain is exactly configured or one of its subdomains."""
    normalized = domain.casefold().rstrip(".")
    return any(normalized == configured or normalized.endswith(f".{configured}") for configured in configured_domains)


def configured_account_scope(config: Mapping[str, object], account: str) -> ConfiguredAccountScope:
    """Return one strictly validated configured account scope."""
    raw_accounts = config.get("accounts")
    if not isinstance(raw_accounts, Mapping) or not raw_accounts:
        raise ConfigError("accounts.yaml must define accounts")
    if len(raw_accounts) > MAX_CONFIGURED_ACCOUNTS:
        raise ConfigError("accounts.yaml has too many accounts")
    if _ACCOUNT_RE.fullmatch(account) is None:
        raise ConfigError("accounts.yaml has an invalid account key")
    raw_account = raw_accounts.get(account)
    if not isinstance(raw_account, Mapping):
        raise ConfigError("Unknown account; choose one configured account")
    domains = _normalized_domains(raw_account.get("domains"), field="account domains", required=True)
    internal_domains = _normalized_domains(
        config.get("internal_domains", []),
        field="internal domains",
        required=False,
    )
    return ConfiguredAccountScope(account, domains, internal_domains)


def scan_row_budget(limit: int) -> int:
    """Return the deterministic row-inspection budget for one Gmail read."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_QUERY_LIMIT:
        raise ValueError("limit must be between 1 and 500")
    return min(MAX_SCAN_ROWS, max(MIN_SCAN_ROWS, limit * SCAN_ROWS_PER_RESULT))


def markdown_cell(value: str) -> str:
    """Escape one already-bounded value for a Markdown table cell."""
    escaped = escape(value, quote=False).replace("\\", "\\\\")
    for character in "|[]()!":
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


@contextmanager
def sqlite_query_budget(
    connection: sqlite3.Connection,
    *,
    row_budget: int,
    steps_per_row: int | None = None,
) -> Iterator[SQLiteQueryBudget]:
    """Install and always remove one SQLite virtual-machine work budget."""
    if isinstance(row_budget, bool) or row_budget < 1:
        raise ValueError("row_budget must be a positive integer")
    resolved_steps_per_row = DEFAULT_SQLITE_STEPS_PER_ROW if steps_per_row is None else steps_per_row
    if isinstance(resolved_steps_per_row, bool) or resolved_steps_per_row < 1:
        raise ValueError("steps_per_row must be a positive integer")
    state = SQLiteQueryBudget(min(MAX_SQLITE_STEPS, row_budget * resolved_steps_per_row))
    connection.set_progress_handler(state.progress, SQLITE_PROGRESS_INTERVAL)
    try:
        yield state
    finally:
        connection.set_progress_handler(None, 0)
