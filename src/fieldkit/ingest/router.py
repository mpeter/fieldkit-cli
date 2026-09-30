"""Account and pursuit router for the fieldkit ingest pipeline.

Provides two public functions:

- route_by_domains(): maps a list of email domains to an account name,
  confidence level, and internal-only flag.
- route_with_pursuits(): extends route_by_domains() with pursuit-level
  matching when a keyword list is supplied.
"""

import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from fieldkit.config import ConfigError, get_accounts_config, get_fieldkit_home
from fieldkit.errors import RoutingReadRetryableError
from fieldkit.pursuit.io import read_pursuit_text_snapshot
from fieldkit.util.workspace_paths import resolve_workspace_output

_MAX_PURSUIT_DIRECTORY_ENTRIES = 10_000
_PURSUIT_HEADING_CHARACTERS = 500

# ---------------------------------------------------------------------------
# Product name normalization (historic regression)
# Maps spoken/transcription variants → canonical slug fragment.
# Gemini speech-to-text renders product names inconsistently; normalising
# before pursuit matching ensures "RoAI" matches "rhoai-2026-services".
# ---------------------------------------------------------------------------
# The built-in aliases cover common transcription variants. Organizations can
# supply their own pursuit keywords without changing this table.
_PRODUCT_ALIASES: dict[str, str] = {
    # OpenShift AI (RHOAI)
    "rhoai": "rhoai",
    "roai": "rhoai",
    "ro ai": "rhoai",
    "rho ai": "rhoai",
    "r-oai": "rhoai",
    "roy ai": "rhoai",  # Gemini hallucination
    "row ai": "rhoai",  # Gemini hallucination
    "openshift ai": "rhoai",
    "open shift ai": "rhoai",
    "openshift data science": "rhoai",  # OPL former name
    # Ansible Automation Platform (AAP)
    "aap": "aap",
    "ansible automation platform": "aap",
    # Event-Driven Ansible (EDA)
    "eda": "eda",
    "event driven ansible": "eda",
    "event-driven ansible": "eda",
    # Automation Execution Platform — customer-internal term for AEP
    "aep": "aep",
    "automation execution platform": "aep",
    "ansible execution platform": "aep",
    # OpenShift Container Platform (RHOCP)
    "rhocp": "rhocp",
    "ocp": "rhocp",
    "openshift container platform": "rhocp",
    "openshift enterprise": "rhocp",  # OPL former name
    # OpenShift Virtualization
    "ocp virt": "ocp-virt",
    "openshift virt": "ocp-virt",
    "openshift virtualization": "ocp-virt",
    "openshift virtualization engine": "ocp-virt",
    # Enterprise Linux (RHEL)
    "rhel": "rhel",
    # Enterprise Linux AI
    "rhel ai": "rhel-ai",
    # AI Enterprise (RHAIE)
    "rhaie": "rhaie",
    "ai enterprise": "rhaie",
    # Advanced Cluster Management
    "acm": "acm",
    "advanced cluster management": "acm",
    # Advanced Cluster Security
    "acs": "acs",
    "advanced cluster security": "acs",
    # OpenShift Data Foundation
    "odf": "odf",
    "openshift data foundation": "odf",
    # OpenStack Services on OpenShift
    "rhoso": "rhoso",
    "openstack services on openshift": "rhoso",
    # Developer Hub
    "rhdh": "rhdh",
    "developer hub": "rhdh",
    # Trusted Artifact Signer
    "rhtas": "rhtas",
    "trusted artifact signer": "rhtas",
    "trusted signer": "rhtas",  # OPL former name
    # Trusted Profile Analyzer
    "rhtpa": "rhtpa",
    "trusted profile analyzer": "rhtpa",
    "trusted content": "rhtpa",  # OPL former name
    # JBoss EAP
    "eap": "eap",
    "jboss eap": "eap",
    "jboss": "eap",
    # Quay
    "quay": "quay",
    # Service Interconnect
    "service interconnect": "interconnect",
    "application interconnect": "interconnect",  # OPL former name
}

# historic regression: single-pass regex substitution, longest-alias-first, replaces the
# old sequential text.replace() loop. Sequential replace() corrupted matches
# whenever one alias's canonical output contained a shorter alias's key as a
# substring (e.g. "ocp virt" -> "ocp-virt" still contains "ocp", which the
# "ocp" -> "rhocp" alias then re-matched, producing "rhocp-virt"). A single
# regex pass over the *original* text avoids rescanning replacement output.
_PRODUCT_ALIAS_PATTERN = re.compile(
    "|".join(re.escape(alias) for alias in sorted(_PRODUCT_ALIASES, key=len, reverse=True))
)


def _apply_product_aliases(text: str) -> str:
    """Replace every product alias occurrence in text with its canonical slug fragment."""
    return _PRODUCT_ALIAS_PATTERN.sub(lambda m: _PRODUCT_ALIASES[m.group(0)], text)


# ---------------------------------------------------------------------------
# Pursuit-matching stopwords (historic regression)
# Generic words that appear in many pursuit slugs/H1s and produce false matches.
# Words under 4 chars are already filtered by length; these are longer generics.
# ---------------------------------------------------------------------------
_PURSUIT_STOPWORDS: frozenset[str] = frozenset(
    {
        # Deal/account generic nouns
        "bank",
        "america",
        "services",
        "platform",
        "production",
        "security",
        "strategy",
        "renewal",
        "drawdown",
        "consulting",
        "phase",
        "project",
        "management",
        "automation",
        "infrastructure",
        "implementation",
        "solution",
        "meeting",
        "weekly",
        "connect",
        "sync",
        "update",
        "review",
        "discussion",
        "planning",
        "session",
        "call",
        "notes",
        "transcript",
        "gemini",
        "internal",
        "team",
        # account short-names that appear in stopwords are injected dynamically
        # at runtime from accounts.yaml; static placeholders removed (R23).
        "state",
        "farm",
        "globalpay",  # pii-guard: ignore — generic word, not an account slug
        "ibm",
        "customer",
        "timeline",
        "deployment",
        "environment",
        "cluster",
        "upgrade",
        "transition",
        "extension",
        "direction",
        "approval",
        "evaluation",
        "onboarding",
        "milestones",
        "leadership",
        "finance",
        "funding",
        "wednesday",
        "friday",
        "confirmed",
        "granted",
        "scheduled",
        "approved",
        "definitive",
        # Common short words (< 5 chars) that slip past length filter
        "and",
        "the",
        "for",
        "not",
        "was",
        "are",
        "has",
        "had",
        "its",
        "that",
        "this",
        "with",
        "from",
        "they",
        "their",
        "will",
        "been",
        "when",
        "than",
        "work",
        "plan",
        "take",
        "next",
        "year",
        "tied",
        "end",
        "start",
        "early",
        "about",
        "current",
        "target",
        "likely",
        "rather",
        "through",
        "potential",
        "technical",
        "induction",
        "timing",
        "replacement",
        "pregnancy",
        "computer",
        "battery",
        "phone",
        "laptop",
        "carl",
        "ragu",
        "pedro",
        "brooke",  # first names without account context
    }
)


class Confidence(StrEnum):
    """Confidence level for a routing result."""

    HIGH = "high"
    LOW = "low"
    NONE = "none"


@dataclass(frozen=True)
class RouteResult:
    """Immutable result of a routing operation.

    ``accounts`` is always non-empty: an empty list is coerced to ``["unknown"]``
    by ``__post_init__``. Use ``primary_account()`` in ``pipeline.py`` to safely
    access the first account without a guard expression.
    """

    accounts: list[str]
    confidence: Confidence
    is_internal: bool
    pursuits: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Coerce empty accounts list to ['unknown'] to prevent IndexError at access sites."""
        if not self.accounts:
            # frozen=True prevents direct assignment; use object.__setattr__ to bypass.
            # This is the established pattern for frozen dataclass post-init mutation.
            object.__setattr__(self, "accounts", ["unknown"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalize_pursuit_keywords(keywords: list[str]) -> list[str]:
    """Normalize and filter a list of keywords for pursuit matching.

    Applies product-name aliases (RoAI → rhoai), tokenizes multi-word strings,
    removes stopwords, and drops tokens under 4 characters.  Returns a
    deduplicated list of specific, meaningful tokens suitable for slug matching.

    Examples::

        >>> _normalize_pursuit_keywords(["<account> RHOAI production platform"])
        ['rhoai']
        >>> _normalize_pursuit_keywords(["RoAI implementation timeline"])
        ['rhoai', 'implementation', 'timeline']
    """
    result: list[str] = []
    seen: set[str] = set()

    for kw in keywords:
        text = kw.lower().strip()

        # Apply multi-word aliases first (before tokenizing)
        text = _apply_product_aliases(text)

        # Tokenize
        tokens = text.split()
        for raw_tok in tokens:
            # Strip punctuation edges
            tok = raw_tok.strip(".,;:!?\"'()-")
            if len(tok) < 3:
                continue
            if tok in _PURSUIT_STOPWORDS:
                continue
            if tok not in seen:
                seen.add(tok)
                result.append(tok)

    return result


def _normalize_domains(raw: list[str]) -> list[str]:
    """Lowercase, strip whitespace, and extract the domain portion from
    any full email addresses in *raw*.

    Examples::

        >>> _normalize_domains(["Alice@globalpay.example.COM", "globalpay.example.com"])
        ['globalpay.example.com', 'globalpay.example.com']
        >>> _normalize_domains(["  acme.COM  "])
        ['acme.com']
    """
    result: list[str] = []
    for raw_item in raw:
        normalized = raw_item.strip().lower()
        if "@" in normalized:
            normalized = normalized.rsplit("@", 1)[-1]
        # Strip RFC 5322 angle-bracket trailing '>' from display-name format:
        # e.g. "John Smith <john@acme.example.com>" → rsplit('@') → "acme.example.com>"
        normalized = normalized.rstrip(">").strip()
        if normalized:
            result.append(normalized)
    return result


def _unknown() -> RouteResult:
    return RouteResult(accounts=["unknown"], confidence=Confidence.NONE, is_internal=False)


def _pursuit_haystack(slug: str, h1: str) -> set[str]:
    """Build a word-token set from a pursuit slug and H1 heading.

    Splits the slug on hyphens and the H1 on whitespace/punctuation so that
    keyword matching is word-boundary-safe.  'rate' will not match 'strategy'
    and 'product' will not match 'production'.

    Returns a frozenset of lowercase tokens.
    """
    import re as _re

    slug_tokens = slug.lower().split("-")
    h1_tokens = _re.split(r"[\s\-\-\u2014/,.()\[\]]+", h1.lower())
    return (set(slug_tokens) | set(h1_tokens)) - {""}


def _read_pursuit_h1(path: Path) -> str:
    """View the first 500 characters of a bounded, no-follow pursuit snapshot."""
    try:
        content = read_pursuit_text_snapshot(path).content[:_PURSUIT_HEADING_CHARACTERS]
    except OSError:
        raise RoutingReadRetryableError("Cannot read ingest pursuit input; restore access and retry") from None
    except ValueError:
        raise ConfigError("Invalid or unsafe ingest pursuit input") from None
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _pursuit_path(root: Path, relative_path: str) -> Path:
    """Confine pursuit sources within a stable configured workspace namespace."""
    try:
        return resolve_workspace_output(root, relative_path)
    except (ValueError, OSError, RuntimeError):
        raise ConfigError("Invalid or redirected ingest pursuit path") from None


def _match_configured_pursuits(account_info: dict[str, Any], keywords: list[str], data_root: Path | None) -> list[str]:
    """Inspect one confined, bounded pursuit inventory for both matching APIs.

    Configured workspace aliases are supported. Ancestor directories must stay
    stable during inspection; this is not a hostile same-user rename sandbox.
    """
    relative_dir = account_info.get("pursuit_dir", "")
    if not isinstance(relative_dir, str):
        raise ConfigError("Invalid configured ingest pursuit directory")
    if not relative_dir:
        return []
    root = data_root if data_root is not None else get_fieldkit_home()
    pursuit_dir = _pursuit_path(root, relative_dir)
    try:
        entries = os.scandir(pursuit_dir)
    except FileNotFoundError:
        return []
    except OSError:
        raise RoutingReadRetryableError("Cannot scan ingest pursuit directory; restore access and retry") from None
    paths: list[Path] = []
    try:
        with entries:
            for count, entry in enumerate(entries, start=1):
                if count > _MAX_PURSUIT_DIRECTORY_ENTRIES:
                    raise ConfigError("Ingest pursuit directory exceeds the entry limit")
                if entry.name.endswith(".md"):
                    paths.append(_pursuit_path(root, f"{relative_dir}/{entry.name}"))
    except OSError:
        raise RoutingReadRetryableError("Cannot scan ingest pursuit directory; restore access and retry") from None
    account_keywords = [str(value).lower() for value in (account_info.get("keywords") or [])]
    all_keywords = _normalize_pursuit_keywords(keywords) + account_keywords
    matched: list[str] = []
    for path in sorted(paths):
        slug = path.stem
        if ".template" in slug or "gmail-intel" in slug:
            continue
        tokens = _pursuit_haystack(slug, _read_pursuit_h1(path))
        if any(keyword in tokens for keyword in all_keywords):
            matched.append(slug)
    return matched


def _load_accounts_config(data_root: Path | None) -> dict[str, Any]:
    """Read fresh validated routing configuration for the selected workspace."""
    return get_accounts_config(strict=True, workspace_root=data_root)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def route_by_domains(
    domains: list[str],
    *,
    data_root: Path | None = None,
) -> RouteResult:
    """Map a list of email domains (or full addresses) to an account routing result.

    Algorithm:
    1. Normalise domains → lowercase, extract domain portion.
    2. Empty after filtering → unknown/NONE.
    3. ALL domains are internal → resolves the account with ``internal: true`` from accounts.yaml;
       falls back to the generic ``"internal"`` pseudo-key when none is configured.
       confidence=NONE, is_internal=True.
    4. Strip internal domains; scan remaining against accounts.yaml.
    5. 0 external matches → unknown/NONE.
       1 external match → HIGH.
       2+ distinct matches → LOW (all matched account names).

    Errors from get_accounts_config() propagate
    to the caller — they are NOT swallowed here.
    """
    normed = _normalize_domains(domains)
    if not normed:
        return _unknown()

    # Internal and external classifications use the same validated snapshot.
    cfg = _load_accounts_config(data_root)
    internal = set(cfg.get("internal_domains", []))
    accounts_cfg: dict[str, Any] = cfg.get("accounts", {}) if isinstance(cfg, dict) else {}

    if all(d in internal for d in normed):
        internal_slug = next(
            (slug for slug, info in accounts_cfg.items() if isinstance(info, dict) and info.get("internal") is True),
            "internal",
        )
        return RouteResult(accounts=[internal_slug], confidence=Confidence.NONE, is_internal=True)

    external = [d for d in normed if d not in internal]

    matched_accounts: list[str] = []
    for account_name, account_info in accounts_cfg.items():
        if not isinstance(account_info, dict):
            continue
        account_domains: list[str] = account_info.get("domains", []) or []
        account_domain_set = {str(d).lower() for d in account_domains}
        if any(d in account_domain_set for d in external):
            matched_accounts.append(str(account_name))

    if not matched_accounts:
        return _unknown()
    if len(matched_accounts) == 1:
        return RouteResult(accounts=matched_accounts, confidence=Confidence.HIGH, is_internal=False)
    return RouteResult(accounts=matched_accounts, confidence=Confidence.LOW, is_internal=False)


def route_by_title(
    title: str,
    *,
    data_root: Path | None = None,
) -> RouteResult:
    """Route a meeting by matching its title against account keyword lists.

    Used as a fallback when domain-based routing returns *unknown* (e.g. when
    meeting invitee emails are absent from the Gemini Notes document).

    Algorithm:
    1. Lowercase the title.
    2. For each account in accounts.yaml, check whether any of its ``keywords``
       match the lowercased title as whole words (word-boundary regex, not substring).
    3. 0 matches → unknown/NONE.
       1 match → accounts=[name], confidence=LOW (title is weaker signal than domain).
       2+ matches → LOW, all matched account names.

    Confidence is always LOW even for a single match, because title keyword
    matching is inherently fuzzier than domain matching.

    Errors from get_accounts_config() propagate to the caller.
    """
    if not title or not title.strip():
        return _unknown()

    title_lower = title.lower()

    cfg = _load_accounts_config(data_root)
    accounts_cfg: dict[str, Any] = cfg.get("accounts", {}) if isinstance(cfg, dict) else {}

    matched_accounts: list[str] = []
    for account_name, account_info in accounts_cfg.items():
        if not isinstance(account_info, dict):
            continue
        keywords: list[str] = [str(k).lower() for k in (account_info.get("keywords") or [])]
        # historic regression: word-boundary match, not raw substring — a short keyword like
        # "ai" must not match inside an unrelated word like "daily".
        if any(re.search(rf"\b{re.escape(kw)}\b", title_lower) for kw in keywords):
            matched_accounts.append(str(account_name))

    if not matched_accounts:
        return _unknown()
    return RouteResult(
        accounts=matched_accounts,
        confidence=Confidence.LOW,
        is_internal=False,
    )


def route_by_content(content: str, *, data_root: Path | None = None) -> RouteResult:
    """Route external transcript content using configured whole-word account keywords.

    Ambient sessions have no invite metadata or trustworthy title. The caller must
    accept the result only when exactly one account matches.
    """
    return route_by_title(content, data_root=data_root)


def route_with_pursuits(
    domains: list[str],
    *,
    keywords: list[str] | None = None,
    data_root: Path | None = None,
) -> RouteResult:
    """Extend route_by_domains() with pursuit-level keyword matching.

    Only performs pursuit scanning when confidence is HIGH (unambiguous single
    account match).  For all other confidence levels the bare RouteResult from
    route_by_domains() is returned unchanged.

    Pursuit matching:
    - Inspects a bounded, confined ``<pursuit_dir>/*.md`` inventory, skipping files whose stem contains
      '.template' or 'gmail-intel'.
    - For each file, extracts the slug (stem) and the first H1 heading from
      the first 500 characters of the shared bounded pursuit snapshot.
    - A pursuit is included when *any* of the following keyword lists has at
      least one case-insensitive match against slug or H1:
        * the explicit *keywords* parameter (if provided)
        * the account's ``keywords`` list from accounts.yaml (always checked)

    If neither keyword source matches any pursuit, ``pursuits`` is empty.

    Config errors propagate to the caller.
    """
    base = route_by_domains(domains, data_root=data_root)

    if base.confidence != Confidence.HIGH:
        return base

    account_name = base.accounts[0]

    cfg = _load_accounts_config(data_root)
    accounts_cfg: dict[str, Any] = cfg.get("accounts", {}) if isinstance(cfg, dict) else {}
    account_info: dict[str, Any] = accounts_cfg.get(account_name, {}) or {}

    matched_pursuits = _match_configured_pursuits(account_info, list(keywords or []), data_root)

    return RouteResult(
        accounts=base.accounts,
        confidence=base.confidence,
        is_internal=base.is_internal,
        pursuits=matched_pursuits,
    )


def match_pursuits_for_account(
    account_name: str,
    keywords: list[str],
    *,
    data_root: Path | None = None,
) -> list[str]:
    """Return pursuit slugs for *account_name* whose slug or H1 matches any keyword.

    Used when the account is already known (e.g. from title routing) but no
    email domains are available to drive route_with_pursuits.

    Args:
        account_name: Account key from accounts.yaml (e.g. '<account-slug>').
        keywords:     Case-insensitive substrings to match against pursuit
                      slug + H1.  Account-level keywords from accounts.yaml
                      are always appended automatically.
        data_root:    Override for the data root path (used in tests).

    Returns:
        Sorted list of matching pursuit stem names, or [] if none match.
    """
    cfg = _load_accounts_config(data_root)
    accounts_cfg: dict[str, Any] = cfg.get("accounts", {}) if isinstance(cfg, dict) else {}
    account_info: dict[str, Any] = accounts_cfg.get(account_name, {}) or {}

    return _match_configured_pursuits(account_info, keywords, data_root)
