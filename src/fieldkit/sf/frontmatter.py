"""Confined, snapshot-bound Salesforce frontmatter updates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from typing import Any, Literal

from fieldkit.errors import FieldkitError, SalesforceSyncPartialError
from fieldkit.pursuit.io import read_pursuit_text_snapshot, render_frontmatter_raw, write_frontmatter_raw
from fieldkit.pursuit.models import canonicalize_legacy_meddpicc
from fieldkit.pursuit.paths import PursuitPathError, resolve_pursuit_file
from fieldkit.pursuit.validation import parse_pursuit_content, pursuit_schema_path, validate_pursuit_content
from fieldkit.sf.opportunities import is_opportunity_id, validate_opportunity_binding
from fieldkit.sf.reconciliation import TableChange, reconcile_key_fields
from fieldkit.util.workspace_paths import resolve_workspace_output

RecordKind = Literal["opportunity", "account"]
_PUBLICATION_LOCK_TIMEOUT_SECONDS = 5

OPPORTUNITY_FIELD_MAP: dict[str, str] = {
    "sf_opportunity_id": "opportunity_id",
    "sf_name": "name",
    "sf_stage": "stage",
    "sf_close_date": "close_date",
    "sf_arr": "arr",
    "sf_owner": "owner",
    "sf_next_steps": "next_steps",
    "sf_last_pulled": "pulled_at",
    "sf_acv": "acv",
    "sf_consulting_acv": "consulting_acv",
    "sf_training_acv": "training_acv",
    "sf_opportunity_number": "opportunity_number",
    "sf_contract_type": "contract_type",
}
ACCOUNT_FIELD_MAP: dict[str, str] = {
    "sf_industry": "industry",
    "sf_owner": "owner",
    "sf_open_opps": "open_opportunity_count",
    "sf_pulled_at": "pulled_at",
}
_DEAL_SPLITS_KEY = "sf_deal_splits"
_MONETARY_FIELDS = frozenset({"sf_acv", "sf_arr", "sf_consulting_acv", "sf_training_acv"})
_DOLLAR_RE = re.compile(r"\$[\d,]+(?:\.\d+)?\Z")
_ACCOUNT_ID_RE = re.compile(r"001(?:[A-Za-z0-9]{12}|[A-Za-z0-9]{15})\Z")
_OPPORTUNITY_ONLY_INPUTS = frozenset(OPPORTUNITY_FIELD_MAP.values()) - {"owner", "pulled_at"}
_ACCOUNT_ONLY_INPUTS = frozenset(ACCOUNT_FIELD_MAP.values()) - {"owner", "pulled_at"}
_OPPORTUNITY_METADATA_INPUTS = frozenset(
    {
        "owner_email",
        "application_services_acv",
        "services_total",
        "sf_account_name",
        "sf_account_industry",
        "sf_account_sf_id",
        "sf_identify_pain",
        "sf_decision_criteria",
        "sf_main_competitor",
        "sf_closed_lost_reason",
        "sf_probability",
    }
)
_ACCOUNT_METADATA_INPUTS = frozenset(
    {"account_id", "account_name", "segment", "owner_email", "open_consulting_acv", "open_training_acv"}
)
_ALL_DIRECT_FIELDS = {**OPPORTUNITY_FIELD_MAP, **ACCOUNT_FIELD_MAP}
_MONETARY_INPUTS = frozenset(OPPORTUNITY_FIELD_MAP[key] for key in _MONETARY_FIELDS)
_TEXT_INPUTS = (
    (frozenset(OPPORTUNITY_FIELD_MAP.values()) | frozenset(ACCOUNT_FIELD_MAP.values()))
    - _MONETARY_INPUTS
    - {"opportunity_id", "open_opportunity_count"}
)
_NONFINITE_TEXT = frozenset({"nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"})


@dataclass(frozen=True)
class SalesforceFrontmatterPayload:
    """Validated Salesforce values for one unambiguous record kind."""

    record_kind: RecordKind
    values: dict[str, Any]


@dataclass(frozen=True)
class SalesforceFrontmatterResult:
    """Observable result of one frontmatter preview or update."""

    path: Path
    written: bool
    record_kind: RecordKind
    keys: tuple[str, ...]
    stage_drift: bool
    legacy_migration: bool
    name_slug_diverges: bool


@dataclass(frozen=True)
class _PublicationPlan:
    """Exact validated bytes and source snapshot required for publication."""

    target: Path
    frontmatter: dict[str, Any]
    body: str
    expected_mtime: float
    source: str
    rendered: str


@dataclass(frozen=True)
class SalesforceReconciliationResult:
    """Outcome of a preview or guarded table publication."""

    path: Path
    status: Literal["updated", "ok", "skip"]
    changes: tuple[TableChange, ...]


def parse_salesforce_frontmatter_payload(raw: object) -> SalesforceFrontmatterPayload:
    """Validate the accepted API-envelope or direct-field payload."""
    if not isinstance(raw, dict):
        raise FieldkitError("Salesforce frontmatter payload must be a JSON object")
    if not raw:
        raise FieldkitError("Empty Salesforce frontmatter payload would clear existing fields")
    values = _normalize_direct_fields(raw)
    if values.get("status", "ok") != "ok":
        raise FieldkitError("Salesforce frontmatter payload status is not ok")

    has_account = "account_id" in values or bool(_ACCOUNT_ONLY_INPUTS.intersection(values))
    has_opportunity = "opportunity_id" in values or bool(_OPPORTUNITY_ONLY_INPUTS.intersection(values))
    if has_account and has_opportunity:
        raise FieldkitError("Salesforce frontmatter payload mixes account and opportunity identity")
    record_kind: RecordKind = "account" if has_account else "opportunity"
    field_map = ACCOUNT_FIELD_MAP if record_kind == "account" else OPPORTUNITY_FIELD_MAP
    recognized = set(field_map.values()) | {"status"}
    if record_kind == "opportunity":
        recognized |= {"deal_splits"} | _OPPORTUNITY_METADATA_INPUTS
    else:
        recognized |= _ACCOUNT_METADATA_INPUTS
    unknown = set(values) - recognized
    if unknown:
        raise FieldkitError("Salesforce frontmatter payload contains unsupported fields")
    if not (set(field_map.values()) - {"opportunity_id"}).intersection(values):
        raise FieldkitError("Salesforce frontmatter payload contains no recognized data fields")

    if record_kind == "opportunity" and "opportunity_id" in values:
        opportunity_id = values["opportunity_id"]
        if not isinstance(opportunity_id, str) or not is_opportunity_id(opportunity_id):
            raise FieldkitError("Salesforce opportunity identity is invalid")
    if record_kind == "account":
        account_id = values.get("account_id")
        if account_id not in (None, "") and (
            not isinstance(account_id, str) or _ACCOUNT_ID_RE.fullmatch(account_id) is None
        ):
            raise FieldkitError("Salesforce account identity is invalid")
    _validate_rendered_field_values(values)
    if "deal_splits" in values:
        values["deal_splits"] = _normalize_deal_splits(values["deal_splits"])
    return SalesforceFrontmatterPayload(record_kind=record_kind, values=values)


def _validate_rendered_field_values(values: dict[str, Any]) -> None:
    """Reject values that cannot be represented by the supported field contract."""
    for field in _TEXT_INPUTS.intersection(values):
        value = values[field]
        if value is not None and not isinstance(value, str):
            raise FieldkitError(f"Salesforce {field} must be a string or null")
    for field in _MONETARY_INPUTS.intersection(values):
        value = values[field]
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip().lower() in _NONFINITE_TEXT:
                raise FieldkitError(f"Salesforce {field} must be finite")
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FieldkitError(f"Salesforce {field} must be a string, number, or null")
        if not isfinite(value):
            raise FieldkitError(f"Salesforce {field} must be finite")
    if "open_opportunity_count" in values:
        count = values["open_opportunity_count"]
        if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
            raise FieldkitError("Salesforce open_opportunity_count must be a non-negative integer")


def _normalize_direct_fields(raw: dict[object, object]) -> dict[str, Any]:
    """Copy string-keyed input and translate direct ``sf_*`` field names."""
    normalized: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise FieldkitError("Salesforce frontmatter payload keys must be strings")
        payload_key = _ALL_DIRECT_FIELDS.get(key, key)
        if payload_key in normalized:
            raise FieldkitError("Salesforce frontmatter payload defines one field more than once")
        normalized[payload_key] = value
    normalized.setdefault("status", "ok")
    return normalized


def _normalize_deal_splits(raw: object) -> list[dict[str, object]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise FieldkitError("Salesforce deal_splits must be a list")
    normalized: list[dict[str, object]] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise FieldkitError(f"deal_splits[{index}] must be an object")
        offering = entry.get("offering", "")
        if not isinstance(offering, str):
            raise FieldkitError(f"deal_splits[{index}].offering must be a string")
        raw_percentage = entry.get("pct", 0.0)
        if isinstance(raw_percentage, bool):
            raise FieldkitError(f"deal_splits[{index}].pct must be numeric")
        try:
            percentage = float(raw_percentage)
        except (TypeError, ValueError):
            raise FieldkitError(f"deal_splits[{index}].pct must be numeric") from None
        if not isfinite(percentage):
            raise FieldkitError(f"deal_splits[{index}].pct must be finite")
        normalized.append({"offering": offering, "pct": percentage})
    return normalized


def update_salesforce_frontmatter(
    path: Path,
    *,
    workspace: Path,
    payload: SalesforceFrontmatterPayload,
    dry_run: bool,
    expected_opportunity_id: str | None,
) -> SalesforceFrontmatterResult:
    """Preview or write one confined Salesforce frontmatter update."""
    target = _resolve_target(path, workspace=workspace, record_kind=payload.record_kind)
    try:
        snapshot = read_pursuit_text_snapshot(target)
    except (FileNotFoundError, ValueError):
        raise FieldkitError("Salesforce frontmatter target is not a stable UTF-8 file") from None
    document = parse_pursuit_content(snapshot.content)
    if document is None:
        raise FieldkitError("Salesforce frontmatter target has no frontmatter block")
    values, updated, field_map = _prepare_frontmatter_values(
        document.frontmatter,
        payload,
        expected_opportunity_id=expected_opportunity_id,
    )
    body = reconcile_key_fields(document.body, updated).body if payload.record_kind == "opportunity" else document.body
    rendered = _render_validated_update(
        target,
        updated,
        body,
        expected_mtime=snapshot.info.st_mtime,
        record_kind=payload.record_kind,
    )
    result = SalesforceFrontmatterResult(
        path=target,
        written=not dry_run,
        record_kind=payload.record_kind,
        keys=tuple(field_map),
        stage_drift=_stage_drift(updated),
        legacy_migration="meddpicc" in document.frontmatter,
        name_slug_diverges=_name_slug_diverges(values, target),
    )
    if dry_run:
        return result
    _publish_validated_update(
        _PublicationPlan(
            target=target,
            frontmatter=updated,
            body=body,
            expected_mtime=snapshot.info.st_mtime,
            source=snapshot.content,
            rendered=rendered,
        )
    )
    return result


def reconcile_salesforce_pursuit(path: Path, *, workspace: Path, dry_run: bool) -> SalesforceReconciliationResult:
    """Reconcile one parsed pursuit through the same snapshot-bound publisher."""
    target = _resolve_target(path, workspace=workspace, record_kind="opportunity")
    try:
        snapshot = read_pursuit_text_snapshot(target)
    except (FileNotFoundError, ValueError):
        raise FieldkitError("Salesforce frontmatter target is not a stable UTF-8 file") from None
    document = parse_pursuit_content(snapshot.content)
    if document is None:
        raise FieldkitError("Salesforce frontmatter target has no frontmatter block")
    table = reconcile_key_fields(document.body, document.frontmatter)
    status: Literal["updated", "ok", "skip"] = (
        "updated" if table.changes else "ok" if any(key.startswith("sf_") for key in document.frontmatter) else "skip"
    )
    if not table.changes:
        return SalesforceReconciliationResult(target, status, ())
    rendered = _render_validated_update(
        target,
        document.frontmatter,
        table.body,
        expected_mtime=snapshot.info.st_mtime,
        record_kind="opportunity",
    )
    if not dry_run:
        _publish_validated_update(
            _PublicationPlan(
                target, document.frontmatter, table.body, snapshot.info.st_mtime, snapshot.content, rendered
            )
        )
    return SalesforceReconciliationResult(target, status, table.changes)


def _prepare_frontmatter_values(
    original: dict[str, Any],
    payload: SalesforceFrontmatterPayload,
    *,
    expected_opportunity_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    """Bind identity and assemble the unrendered frontmatter mapping."""
    try:
        canonicalize_legacy_meddpicc(original)
    except ValueError:
        raise FieldkitError("Salesforce frontmatter target has invalid historical qualification data") from None
    updated = dict(original)
    values = dict(payload.values)
    if payload.record_kind == "opportunity":
        _bind_opportunity_identity(values, updated, expected_opportunity_id=expected_opportunity_id)
    field_map = ACCOUNT_FIELD_MAP if payload.record_kind == "account" else OPPORTUNITY_FIELD_MAP
    for frontmatter_key, payload_key in field_map.items():
        incoming = values.get(payload_key)
        if payload_key == "pulled_at":
            incoming = incoming or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        updated[frontmatter_key] = _frontmatter_value(incoming, updated.get(frontmatter_key), frontmatter_key)
    if payload.record_kind == "opportunity":
        updated[_DEAL_SPLITS_KEY] = values.get("deal_splits", [])
    return values, updated, field_map


def _bind_opportunity_identity(
    values: dict[str, Any],
    frontmatter: dict[str, Any],
    *,
    expected_opportunity_id: str | None,
) -> None:
    """Require an existing or explicit opportunity identity and bind sync calls."""
    existing_id = frontmatter.get("sf_opportunity_id")
    if "opportunity_id" not in values and existing_id not in (None, ""):
        values["opportunity_id"] = existing_id
    resolved_id = values.get("opportunity_id")
    if resolved_id in (None, ""):
        raise FieldkitError("Salesforce opportunity identity is required")
    if not isinstance(resolved_id, str) or not is_opportunity_id(resolved_id):
        raise FieldkitError("Salesforce opportunity identity is invalid")
    validate_opportunity_binding(expected_opportunity_id or resolved_id, resolved_id, existing_id)


def _render_validated_update(
    target: Path,
    frontmatter: dict[str, Any],
    body: str,
    *,
    expected_mtime: float,
    record_kind: RecordKind,
) -> str:
    """Render one exact candidate and validate opportunity schema constraints."""
    try:
        rendered = render_frontmatter_raw(target, frontmatter, body, expected_mtime=expected_mtime)
    except ValueError:
        raise FieldkitError("Salesforce frontmatter could not be rendered safely") from None
    if record_kind == "opportunity":
        errors = validate_pursuit_content(rendered, schema_path=pursuit_schema_path())
        if errors:
            raise FieldkitError("Salesforce frontmatter failed pursuit schema validation: " + "; ".join(errors))
    return rendered


def _publish_validated_update(plan: _PublicationPlan) -> None:
    """Publish one exact candidate while retaining retryable failure semantics."""
    try:
        write_frontmatter_raw(
            plan.target,
            plan.frontmatter,
            plan.body,
            expected_mtime=plan.expected_mtime,
            validated_content=plan.rendered,
            validated_source_content=plan.source,
            timeout_seconds=_PUBLICATION_LOCK_TIMEOUT_SECONDS,
        )
    except OSError:
        raise SalesforceSyncPartialError("Salesforce frontmatter publication could not be verified") from None
    except ValueError:
        raise FieldkitError("Salesforce frontmatter publication was refused") from None


def _resolve_target(path: Path, *, workspace: Path, record_kind: RecordKind) -> Path:
    try:
        if record_kind == "opportunity":
            return resolve_pursuit_file(workspace, str(path))
        root = workspace.resolve(strict=True)
        relative = _workspace_relative_account_path(workspace, root, path)
        if len(relative.parts) != 3 or relative.parts[0] != "accounts" or relative.parts[2] != "account.md":
            raise ValueError
        target = resolve_workspace_output(workspace, relative.as_posix())
        if not target.is_file():
            raise ValueError
        return target
    except (OSError, RuntimeError, PursuitPathError, ValueError):
        raise FieldkitError("Salesforce frontmatter target is not an approved workspace location") from None


def _workspace_relative_account_path(workspace: Path, resolved_workspace: Path, candidate: Path) -> Path:
    """Return an account target relative to either configured workspace spelling."""
    candidate_paths = (
        (candidate.absolute(),)
        if candidate.is_absolute()
        else ((Path.cwd() / candidate).absolute(), (workspace / candidate).absolute())
    )
    for value in candidate_paths:
        for base in (workspace.absolute(), resolved_workspace):
            if value.is_relative_to(base):
                return value.relative_to(base)
    raise ValueError("Account target is outside workspace")


def _frontmatter_value(incoming: object, existing: object, key: str) -> object:
    if incoming is None or (isinstance(incoming, str) and incoming.lower() in {"null", "none"}):
        return ""
    if key in _MONETARY_FIELDS and isinstance(existing, str) and _DOLLAR_RE.fullmatch(existing):
        try:
            if float(existing.removeprefix("$").replace(",", "")) == float(
                str(incoming).removeprefix("$").replace(",", "")
            ):
                return existing
        except ValueError:
            pass
    return str(incoming)


def _stage_drift(frontmatter: dict[str, Any]) -> bool:
    local = frontmatter.get("stage")
    remote = frontmatter.get("sf_stage")
    if not isinstance(local, str) or not isinstance(remote, str):
        return False
    return _normalize_stage(local) != _normalize_stage(remote)


def _normalize_stage(value: str) -> str:
    """Normalize local and Salesforce stages for drift reporting."""
    return value.strip().lower().replace(" ", "-")


def _name_slug_diverges(values: dict[str, Any], path: Path) -> bool:
    name = values.get("name")
    if not isinstance(name, str) or not name:
        return False
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return bool(slug and slug != path.stem)
