"""fieldkit.pursuit.audit — Deterministic pursuit frontmatter validation.

Validates pursuit files for:
  - Frontmatter structure (required fields, allowed values)
  - SF field naming compliance (underscores, not hyphens)
  - Backstory write prohibition (unverified data in record of truth)
  - Current qualification availability without interpreting historical scores
  - SF close-date divergence (overdue, approaching close date vs stage)

Public API:
  audit_file(path, today) → AuditResult
  audit_directory(root, account_filter, today) → list[AuditResult]
  apply_fixes(path) → FixResult
"""

import contextlib
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import yaml

from fieldkit.pursuit.enums import Stage
from fieldkit.pursuit.gate_criteria import ALLOWED_GATE_STATUSES
from fieldkit.pursuit.io import (
    detect_duplicate_yaml_keys,
    parse_frontmatter_fallback,
    split_frontmatter_raw,
    write_frontmatter_raw,
)
from fieldkit.pursuit.models import canonicalize_legacy_meddpicc
from fieldkit.pursuit.stages import ALL_STAGES as ALLOWED_STAGES
from fieldkit.pursuit.stages import CLOSED_STAGES

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REQUIRED_FRONTMATTER = ("stage", "gate-status", "last-transition", "transition-history")

# ALLOWED_STAGES imported from fieldkit.pursuit.stages — single source of truth.
# CLOSED_STAGES imported from fieldkit.pursuit.stages — single source of truth.

# Hyphenated SF field renames: old → new
HYPHEN_RENAMES: dict[str, str] = {
    "sf-opportunity-id": "sf_opportunity_id",
    "sf-stage": "sf_stage",
    "sf-close-date": "sf_close_date",
    "sf-arr": "sf_arr",
    "sf-owner": "sf_owner",
    "sf-next-steps": "sf_next_steps",
    "sf-last-pulled": "sf_last_pulled",
}

# Legacy fields to remove entirely.
# The hyphen form (sf-opportunity-number) is legacy per R05; remove it.
# The underscore form (sf_opportunity_number) is now canonical — do NOT include it here.
LEGACY_REMOVE = frozenset({"sf-opportunity-number"})  # hyphen form only; underscore form is canonical

# Required SF fields (post-fix target)
REQUIRED_SF_FIELDS = (
    "sf_opportunity_id",
    "sf_stage",
    "sf_close_date",
    "sf_arr",
    "sf_owner",
    "sf_next_steps",
    "sf_last_pulled",
)

LATE_STAGES = frozenset(
    {
        "Propose",
        "Negotiate",
        "Closed Won",
        Stage.PROPOSE,
        Stage.NEGOTIATE,
        Stage.CLOSED_WON,
    }
)

# Allowed values for gate-result inside transition-history entries.
# Note: "pending" is NOT valid here — gate-result records a completed gate outcome.
_VALID_GATE_RESULTS: frozenset[str] = frozenset({"pass", "fail", "override"})

_BACKSTORY_RE = re.compile(r"\[backstory", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


def no_files_message(kind: Literal["pursuit", "project"], account: str | None) -> str:
    """Build a standardized "no files found" message (implementation change).

    Never includes an absolute filesystem path — only the account slug, if one
    was passed as a filter. Callers pass their own SystemExit(3) after emitting
    this message; this function only builds the string. `kind` is a closed set
    so mypy catches a typo'd or unsupported noun at the call site instead of it
    silently reaching users in CLI output.
    """
    if account:
        return f"No {kind} files found for account: {account}"
    return f"No {kind} files found."


@dataclass
class Finding:
    level: str  # "ERROR", "WARNING", "CRITICAL"
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.message}"


@dataclass
class AuditResult:
    path: Path
    relative_path: str
    findings: list[Finding] = field(default_factory=list)
    qualification_status: Literal["unavailable"] = "unavailable"
    parse_error: str | None = None
    stage: str = ""

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "ERROR"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "WARNING"]

    @property
    def criticals(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "CRITICAL"]

    @property
    def category(self) -> str:
        if self.parse_error or self.errors:
            return "ERROR"
        if self.criticals or self.warnings:
            return "WARNING"
        return "COMPLIANT"


@dataclass
class FixResult:
    path: Path
    renames: int = 0
    fields_added: int = 0
    legacy_removed: int = 0

    @property
    def total_changes(self) -> int:
        return self.renames + self.fields_added + self.legacy_removed


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_sf_date(raw: Any) -> date | None:
    """Parse M/D/YYYY or YYYY-MM-DD SF date string into a date object."""
    date_value = _date_input_value(raw)
    if date_value is not None:
        return date_value
    return _parse_sf_date_string(raw)


def _parse_sf_date_string(raw: Any) -> date | None:
    """Parse a string Salesforce close date into a date object."""
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw:
        return None
    # M/D/YYYY
    if "/" in raw:
        parts = raw.split("/")
        if len(parts) == 3:
            try:
                m, d, y = int(parts[0]), int(parts[1]), int(parts[2])
                return date(y, m, d)
            except (ValueError, TypeError):
                return None
    # YYYY-MM-DD
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _date_input_value(raw: Any) -> date | None:
    """Return native YAML date values before string parsing."""
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    return None


# ---------------------------------------------------------------------------
# Validation rules
# ---------------------------------------------------------------------------


def _check_structure(fm: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    for req in REQUIRED_FRONTMATTER:
        if req not in fm:
            findings.append(Finding("ERROR", f"Missing required field: `{req}`"))

    if "stage" in fm:
        stage = str(fm["stage"]).lower() if fm["stage"] else ""
        if stage not in ALLOWED_STAGES:
            findings.append(
                Finding("ERROR", f"`stage` value '{fm['stage']}' not in allowed set: {sorted(ALLOWED_STAGES)}")
            )

    if "gate-status" in fm:
        gs = str(fm["gate-status"]).lower() if fm["gate-status"] else ""
        if gs not in ALLOWED_GATE_STATUSES:
            findings.append(
                Finding(
                    "ERROR",
                    f"`gate-status` value '{fm['gate-status']}' not in allowed set: {sorted(ALLOWED_GATE_STATUSES)}",
                )
            )

    return findings


def _check_transition_history(fm: dict[str, Any]) -> list[Finding]:
    """Validate gate-result values inside transition-history entries.

    Each entry may optionally carry a `gate-result` field.  When present it
    must be one of the values in _VALID_GATE_RESULTS.  Invalid values (e.g.
    "closed", "clear", "manual") written by external agents are flagged as
    WARNING findings so they surface in audit output without blocking the file
    from being read.
    """
    findings: list[Finding] = []
    history = fm.get("transition-history", [])
    if not isinstance(history, list):
        return findings

    for i, entry in enumerate(history):
        if not isinstance(entry, dict):
            continue
        gate_result = entry.get("gate-result")
        if gate_result is None:
            continue
        if gate_result not in _VALID_GATE_RESULTS:
            stage_info = entry.get("to") or entry.get("from") or f"entry[{i}]"
            findings.append(
                Finding(
                    "WARNING",
                    f"`transition-history` entry (stage: '{stage_info}') has invalid `gate-result` value"
                    f" '{gate_result}' — must be one of {sorted(_VALID_GATE_RESULTS)}",
                )
            )

    return findings


def _check_sf_naming(fm: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    for old_key in HYPHEN_RENAMES:
        if old_key in fm:
            new_key = HYPHEN_RENAMES[old_key]
            if old_key == "sf-opportunity-id":
                findings.append(
                    Finding(
                        "ERROR", f"Field `{old_key}` must be `{new_key}` (hyphenated key is not read by any automation)"
                    )
                )
            else:
                findings.append(
                    Finding("WARNING", f"Field `{old_key}` is a legacy hyphenated variant; rename to `{new_key}`")
                )
    return findings


def _check_backstory(fm: dict[str, Any], body: str) -> list[Finding]:
    findings: list[Finding] = []
    for key, val in fm.items():
        if isinstance(val, str) and _BACKSTORY_RE.search(val):
            excerpt = val[:80].replace("\n", " ")
            findings.append(Finding("ERROR", f"Field `{key}` contains Backstory-derived data: '{excerpt}...'"))

    # Body content checks
    if re.search(r"\(via Backstory\)", body, re.IGNORECASE):
        findings.append(Finding("WARNING", "Body contains '(via Backstory)' attribution — remove or rephrase"))
    if re.search(r"^#+\s*Backstory Activity", body, re.MULTILINE | re.IGNORECASE):
        findings.append(Finding("WARNING", "Body contains 'Backstory Activity' section header — remove"))
    if re.search(r"engagement score", body, re.IGNORECASE):
        findings.append(Finding("WARNING", "Body contains 'Engagement Score' metric — remove"))

    return findings


def _check_historical_qualification(fm: dict[str, Any]) -> list[Finding]:
    """Validate only the historical envelope, never its archived element values."""
    try:
        canonicalize_legacy_meddpicc(fm)
    except ValueError as exc:
        return [Finding("ERROR", f"Invalid historical qualification: {exc}")]
    return []


def _check_close_date_upcoming(
    fm: dict[str, Any],
    days_until: int,
    sf_stage_str: str,
) -> list[Finding]:
    """Return warnings for deals closing within 30 days."""
    findings: list[Finding] = []
    next_steps = fm.get("sf_next_steps") or fm.get("sf-next-steps") or ""

    if 0 <= days_until <= 30:
        if sf_stage_str not in LATE_STAGES:
            findings.append(
                Finding("WARNING", f"Close date in {days_until}d but SF stage is '{sf_stage_str}' — timeline risk")
            )
        if not str(next_steps).strip():
            findings.append(
                Finding(
                    "WARNING",
                    f"Close date in {days_until}d but sf_next_steps is empty — add a current next action",
                )
            )

    return findings


def _check_close_date(fm: dict[str, Any], today: date) -> list[Finding]:
    findings: list[Finding] = []
    raw_date = fm.get("sf_close_date") or fm.get("sf-close-date")
    raw_stage = fm.get("sf_stage") or fm.get("sf-stage") or fm.get("stage") or ""
    fm_stage = str(fm.get("stage", "")).lower()

    # Skip closed pursuits and pre-pipeline
    if _skip_close_date_check(fm_stage):
        return findings

    close_date = _parse_sf_date(raw_date)
    if close_date is None:
        return findings  # No date, no checks

    days_until = (close_date - today).days
    sf_stage_str = str(raw_stage)

    if _is_open_opportunity_overdue(days_until, sf_stage_str):
        findings.append(
            Finding("ERROR", f"SF close date overdue: {raw_date} — update sf_close_date or close the opportunity")
        )

    findings.extend(_check_close_date_upcoming(fm, days_until, sf_stage_str))
    return findings


def _skip_close_date_check(stage: str) -> bool:
    """Return whether a pursuit stage does not need close-date validation."""
    return stage in CLOSED_STAGES or stage == Stage.PRE_PIPELINE


def _is_open_opportunity_overdue(days_until: int, sf_stage: str) -> bool:
    """Return whether an overdue close date belongs to an open opportunity."""
    return days_until < 0 and sf_stage.lower() not in {"closed won", "closed lost", Stage.CLOSED_WON, Stage.CLOSED_LOST}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def audit_file(path: Path, today: date | None = None) -> AuditResult:
    """Validate a single pursuit file and return an AuditResult.

    Args:
        path:  Absolute or relative path to the pursuit markdown file.
        today: Reference date for close-date checks (defaults to today).

    Returns:
        AuditResult with all findings populated.
    """
    if today is None:
        today = datetime.now(tz=UTC).date()

    relative_path = str(path)
    result = AuditResult(path=path, relative_path=relative_path)

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        result.parse_error = f"Cannot read file: {exc}"
        return result

    fm, body = parse_frontmatter_fallback(content)
    if fm is None:
        result.parse_error = "No YAML frontmatter found"
        result.findings.append(Finding("ERROR", "Missing frontmatter entirely — no `---` delimited YAML block"))
        return result

    result.stage = str(fm.get("stage", "")).lower()

    result.findings.extend(_check_structure(fm))
    result.findings.extend(_check_transition_history(fm))
    result.findings.extend(_check_sf_naming(fm))
    result.findings.extend(_check_backstory(fm, body))
    result.findings.extend(_check_historical_qualification(fm))
    result.findings.extend(_check_close_date(fm, today))

    return result


def audit_directory(
    root: Path,
    account_filter: str | None = None,
    today: date | None = None,
) -> list[AuditResult]:
    """Audit all pursuit files under root/accounts/*/pursuits/*.md.

    Args:
        root:           Data root directory (contains `accounts/` subdirectory).
        account_filter: If set, only audit files under accounts/<account_filter>/.
        today:          Reference date for close-date checks.

    Returns:
        List of AuditResult, one per file scanned (sorted by relative_path).
    """
    if today is None:
        today = datetime.now(tz=UTC).date()

    accounts_dir = root / "accounts"
    if not accounts_dir.is_dir():
        return []

    pattern = f"{account_filter}/pursuits/*.md" if account_filter else "*/pursuits/*.md"
    results: list[AuditResult] = []

    for path in sorted(accounts_dir.glob(pattern)):
        if path.name in {"gmail-intel.md", "template.md"}:
            continue
        result = audit_file(path, today)
        # Compute relative path from accounts_dir for cleaner display
        with contextlib.suppress(ValueError):
            result.relative_path = str(path.relative_to(accounts_dir))
        results.append(result)

    return results


def check_yaml_duplicates(path: Path) -> AuditResult:
    """Check a single pursuit file for duplicate YAML frontmatter keys.

    Args:
        path: Path to the pursuit markdown file.

    Returns:
        AuditResult with ERROR findings for each duplicate key found.
    """
    result = AuditResult(path=path, relative_path=str(path))

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        result.parse_error = f"Cannot read file: {exc}"
        return result

    raw = split_frontmatter_raw(content)
    if raw is None:
        result.parse_error = "No YAML frontmatter found"
        return result

    fm_text, _ = raw

    try:
        duplicates = detect_duplicate_yaml_keys(fm_text)
    except yaml.YAMLError as exc:
        result.parse_error = f"YAML parse error: {exc}"
        return result

    for dup_key in duplicates:
        result.findings.append(Finding("ERROR", f"Duplicate YAML key: `{dup_key}`"))

    return result


def check_yaml_duplicates_directory(
    root: Path,
    account_filter: str | None = None,
) -> list[AuditResult]:
    """Scan all pursuit files under root/accounts/*/pursuits/*.md for duplicate YAML keys.

    Args:
        root:           Data root directory (contains `accounts/` subdirectory).
        account_filter: If set, only scan files under accounts/<account_filter>/.

    Returns:
        List of AuditResult for files that have findings or errors.
    """
    accounts_dir = root / "accounts"
    if not accounts_dir.is_dir():
        return []

    pattern = f"{account_filter}/pursuits/*.md" if account_filter else "*/pursuits/*.md"
    results: list[AuditResult] = []

    for path in sorted(accounts_dir.glob(pattern)):
        if path.name in {"gmail-intel.md", "template.md"}:
            continue
        result = check_yaml_duplicates(path)
        with contextlib.suppress(ValueError):
            result.relative_path = str(path.relative_to(accounts_dir))
        if result.parse_error or result.findings:
            results.append(result)

    return results


def apply_fixes(path: Path, *, dry_run: bool = False) -> FixResult:
    """Auto-correct common frontmatter issues in a pursuit file.

    Corrections applied:
    - Rename hyphenated SF fields to underscored equivalents
    - Remove legacy sf-opportunity-number (hyphen form) field; the underscore form
      sf_opportunity_number is canonical and must not be removed

    Does NOT add missing required fields (requires human judgment on values).

    Args:
        path: Path to the pursuit markdown file.
        dry_run: Compute correction counts without replacing the file.

    Returns:
        FixResult with counts of changes made.

    Raises:
        OSError: If the file cannot be read or written.
        ValueError: If legacy and canonical aliases contain conflicting values.
    """
    result = FixResult(path=path)
    expected_mtime = path.stat().st_mtime
    content = path.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter_fallback(content)
    if frontmatter is None:
        raise ValueError(f"No YAML frontmatter in {path}")
    corrected, remove_keys = _plan_frontmatter_fixes(frontmatter, path, result)

    if result.total_changes and not dry_run:
        write_frontmatter_raw(
            path,
            corrected,
            body,
            expected_mtime=expected_mtime,
            remove_keys=frozenset(remove_keys),
        )

    return result


def _plan_frontmatter_fixes(
    frontmatter: dict[str, Any],
    path: Path,
    result: FixResult,
) -> tuple[dict[str, Any], set[str]]:
    """Validate aliases and plan canonical frontmatter corrections."""
    for old_key, new_key in HYPHEN_RENAMES.items():
        if old_key in frontmatter and new_key in frontmatter and frontmatter[old_key] != frontmatter[new_key]:
            raise ValueError(f"conflicting values for {old_key} and {new_key} in {path}")
    corrected: dict[str, Any] = {}
    remove_keys: set[str] = set()
    for key, value in frontmatter.items():
        if key in HYPHEN_RENAMES:
            corrected[HYPHEN_RENAMES[key]] = value
            remove_keys.add(key)
            result.renames += 1
        elif key in LEGACY_REMOVE:
            remove_keys.add(key)
            result.legacy_removed += 1
        else:
            corrected[key] = value
    if "meddpicc" in corrected:
        result.legacy_removed += 1
    return corrected, remove_keys
