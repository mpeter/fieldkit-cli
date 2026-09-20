"""Upsert sf_* or email keys into Markdown YAML frontmatter.

Usage via CLI:
    python -m sf_pipeline frontmatter <file.md> <json-string>
    python -m sf_pipeline frontmatter --validate --file <file.md>

Migrated from routines/lib/update-frontmatter.py — all logic preserved.
"""

import importlib.resources
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import jsonschema
import yaml

from fieldkit.cli_exit import EXIT_PARTIAL
from fieldkit.cli_registry import declare_write
from fieldkit.errors import FieldkitError
from fieldkit.pursuit.io import detect_duplicate_yaml_keys as _detect_duplicate_yaml_keys_fm
from fieldkit.pursuit.io import (
    load_pursuit,
    parse_frontmatter,
    render_frontmatter_raw,
    render_raw_key_value,
    write_frontmatter_raw,
)
from fieldkit.pursuit.models import SF_FIELD_NAMES, canonicalize_legacy_meddpicc

logger = logging.getLogger(__name__)

# historic regression (review fix): Salesforce opportunity IDs are 15 or 18 alphanumeric characters.
# Only inject a file-resident sf_opportunity_id into the payload when it matches this
# format — malformed values (e.g. "NEEDS-LOOKUP", "../../etc") are skipped so the
# historic regression guard fires normally and the write is aborted rather than propagating garbage.
_SF_OPP_ID_RE = re.compile(r"^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$")


def _strip_yaml_inline_comment(raw: str) -> str:
    """Strip an inline YAML comment and surrounding quotes from a raw field value.

    Replicates what ``yaml.safe_load`` does for comment stripping without importing
    yaml.  Example: ``'006ABC # needs lookup'`` → ``'006ABC'``.

    Limitation: not safe for values that legitimately contain ``#`` inside a quoted
    string (e.g. ``'"006ABC#123"'``).  Salesforce opportunity IDs are alphanumeric
    only and never contain ``#``, so this limitation is irrelevant in practice.
    """
    return raw.split("#")[0].strip().strip('"').strip("'")


# ── Key maps ──────────────────────────────────────────────────────────────────

SF_OPP_KEY_MAP = {
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
    # historic regression: derived contract type ("fixed_price" | "standard"); consumers
    # re-anchor ACV to net for fixed-price. Not a monetary key.
    "sf_contract_type": "contract_type",
}

_DEAL_SPLITS_KEY = "sf_deal_splits"

SF_ACCOUNT_KEY_MAP = {
    "sf_industry": "industry",
    "sf_owner": "owner",
    "sf_open_opps": "open_opportunity_count",
    "sf_pulled_at": "pulled_at",
}

# historic regression: include SF_ACCOUNT_KEY_MAP keys so _strip_sf_keys removes them before
# writing, preventing duplicate YAML keys on account.md on repeated sync.
#
# historic regression audit: _build_account_write_payload (account.py) emits these payload keys:
#   account_id, account_name, industry, segment, owner, owner_email,
#   open_opportunity_count, open_consulting_acv, open_training_acv, pulled_at.
# Of these, only the keys present in SF_ACCOUNT_KEY_MAP are written as frontmatter:
#   sf_industry (←industry), sf_owner (←owner), sf_open_opps (←open_opportunity_count),
#   sf_pulled_at (←pulled_at).
# The remaining payload keys (account_id, account_name, segment, owner_email,
# open_consulting_acv, open_training_acv) are not in SF_ACCOUNT_KEY_MAP and are
# silently dropped by _build_updated_fm_lines — they are never written to account.md.
# All four written frontmatter keys are already in _ALL_SF_KEYS via SF_ACCOUNT_KEY_MAP.keys().
# Verified against a sample account.md: no duplicate or stray sf_* keys present.
_ALL_SF_KEYS = sorted(SF_FIELD_NAMES | set(SF_ACCOUNT_KEY_MAP.keys()))

# Guard: every key in SF_OPP_KEY_MAP must exist in PursuitFrontmatter.
# sf_opportunity_territory is intentionally absent (not exposed by the API).
# SF_ACCOUNT_KEY_MAP is excluded — it contains account-level keys not in PursuitFrontmatter.
_unknown_opp_keys = set(SF_OPP_KEY_MAP) - SF_FIELD_NAMES
assert not _unknown_opp_keys, f"SF_OPP_KEY_MAP has keys not in PursuitFrontmatter: {_unknown_opp_keys}"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _val(raw: Any) -> str:
    if raw is None:
        return ""
    s = str(raw)
    # Guard: SF API sometimes returns the string "null" for missing fields.
    # Treat it as empty rather than writing the literal string "null".
    if s.lower() in {"null", "none"}:
        return ""
    return s


def _needs_yaml_quote(key: str, value: str) -> bool:
    return (
        # historic regression: empty string must be quoted so it round-trips as "" not null
        value == ""
        or ": " in value
        or value.startswith((":", "{", "[", "|", ">", "!", "&", "*", "#", "?", "-"))
        or value.startswith('"')
        or value.startswith("'")
        # implementation change: dollar-string monetary values (e.g. '$500,000') must be quoted
        # so they round-trip through YAML parsers as strings, not bare scalars.
        or (value.startswith("$") and re.match(r"^\$[\d,]+(\.\d+)?$", value) is not None)
        # historic regression: this schema string field must round-trip through YAML as a string.
        or key == "sf_opportunity_number"
    )


def _yaml_line(key: str, value: str) -> str:
    # historic regression: multi-line values must use a block scalar, not double-quoted strings.
    # Double-quoting a multi-line value that contains colons (e.g. SF NextStep) produces
    # invalid YAML — the parser treats the colon as a key separator inside the block.
    if "\n" in value:
        indented = "\n".join(f"  {ln}" for ln in value.splitlines())
        return f"{key}: |\n{indented}"
    if _needs_yaml_quote(key, value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'{key}: "{escaped}"'
    return f"{key}: {value}"


def _find_frontmatter_bounds(lines: list[str]) -> tuple[int, int]:
    dashes = [i for i, ln in enumerate(lines) if re.fullmatch(r"-{3,}\s*", ln)]
    if len(dashes) < 2:
        raise ValueError("Fewer than two '---' delimiters found; file has no YAML frontmatter.")
    return dashes[0], dashes[1]


def _strip_sf_keys(fm_lines: list[str]) -> list[str]:
    sf_prefix = tuple(f"{k}:" for k in _ALL_SF_KEYS)
    result: list[str] = []
    skip_children = False  # True while consuming indented children of a list-valued sf_* key
    for ln in fm_lines:
        if ln.startswith(sf_prefix):
            skip_children = True  # start consuming this key and any indented children
            continue
        if skip_children:
            # historic regression: consume both list item lines (indented non-empty) AND block scalar
            # lines (indented or blank). A block scalar written by _yaml_line uses "key: |"
            # followed by indented content lines, which may include blank lines between
            # paragraphs. Blank lines must also be consumed or they become orphaned content
            # that makes the reconstructed frontmatter YAML-invalid.
            if ln == "" or ln[0] == " " or ln[0] == "\t":
                continue
            skip_children = False
        result.append(ln)
    return result


def _coerce_dates(obj: Any) -> Any:
    import datetime as _dt

    if isinstance(obj, dict):
        return {k: _coerce_dates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_coerce_dates(v) for v in obj]
    if isinstance(obj, (_dt.datetime, _dt.date)):
        return obj.isoformat()
    return obj


def _get_schema_path() -> Path:
    """Return the path to the bundled pursuit-frontmatter schema.

    Uses importlib.resources so the schema is found correctly whether fieldkit
    is running from source, installed as a wheel, or installed via ``uv tool``.
    The resource is always a real file on disk (not zip-bundled), so the
    traversable path is safe to return directly.
    """
    ref = importlib.resources.files("fieldkit._data").joinpath("pursuit-frontmatter.schema.json")
    # Path(str(ref)): Traversable doesn't subclass Path in Python <3.12, so we
    # must convert explicitly. str() is safe here because the resource is always
    # a real file on disk (wheel artifacts are not zipped by hatchling).
    return Path(str(ref))


def _validate_frontmatter_content(new_content: str, schema_path: Path) -> list[str]:
    try:
        with schema_path.open(encoding="utf-8") as fh:
            schema = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        click.echo(f"WARNING: Could not load schema at {schema_path}: {exc}", err=True)
        return []

    lines = new_content.splitlines()
    dashes = [i for i, ln in enumerate(lines) if re.fullmatch(r"-{3,}\s*", ln)]
    if len(dashes) < 2:
        return []

    fm_text = "\n".join(lines[dashes[0] + 1 : dashes[1]])
    try:
        fm_data = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as exc:
        return [f"YAML parse error: {exc}"]

    fm_data = _coerce_dates(fm_data)
    validator = jsonschema.Draft202012Validator(schema)
    return [f"[{e.json_path}] {e.message}" for e in validator.iter_errors(fm_data)]


def detect_duplicate_yaml_keys(content: str) -> list[str]:
    """Return a list of YAML keys that appear more than once in the frontmatter block.

    Accepts full file content (including ``---`` delimiters).  Scans only the
    first frontmatter block.  Returns an empty list when no duplicates are
    found or when the content has no parseable frontmatter.

    Delegates to ``fieldkit.pursuit.io.detect_duplicate_yaml_keys`` after
    extracting the frontmatter text from the full content.
    """
    lines = content.splitlines()
    try:
        start, end = _find_frontmatter_bounds(lines)
    except ValueError:
        return []
    fm_text = "\n".join(lines[start + 1 : end])
    return _detect_duplicate_yaml_keys_fm(fm_text)


def _assert_single_frontmatter(content: str, path: str) -> None:
    """Abort if *content* would produce a double-frontmatter file (historic regression guard)."""
    lines = content.splitlines()
    dash_indices = [i for i, ln in enumerate(lines) if re.fullmatch(r"-{3,}\s*", ln)]
    if len(dash_indices) >= 4:
        a1, b0 = dash_indices[1], dash_indices[2]
        if b0 == a1 + 1:
            click.echo(
                f"ERROR: double-frontmatter guard triggered for {path} — write aborted. "
                "Report this as a bug with the file contents.",
                err=True,
            )
            raise SystemExit(EXIT_PARTIAL)


def _collapse_double_frontmatter(content: str) -> str:
    """Collapse two sequential YAML frontmatter blocks into one.

    Detects the pattern::

        ---
        key1: val1
        ---
        ---            <- spurious second open (historic regression)
        key2: val2
        ---
        body...

    When found, merges both key sets (second block wins on collision) and
    writes a single frontmatter block, then the body.

    Returns *content* unchanged when no double-frontmatter is detected.
    """
    import yaml

    lines = content.splitlines()
    dash_indices = [i for i, ln in enumerate(lines) if re.fullmatch(r"-{3,}\s*", ln)]
    if len(dash_indices) < 4:
        return content  # can't have two full frontmatter blocks

    a0, a1, b0, b1 = dash_indices[0], dash_indices[1], dash_indices[2], dash_indices[3]
    # Two blocks are adjacent when the second opening --- immediately follows the first closing ---
    if b0 != a1 + 1:
        return content  # not adjacent — not the double-frontmatter pattern

    click.echo(
        "[frontmatter] WARNING: duplicate frontmatter detected — collapsing into one block.",
        err=True,
    )

    fm1_text = "\n".join(lines[a0 + 1 : a1])
    fm2_text = "\n".join(lines[b0 + 1 : b1])
    body_lines = lines[b1 + 1 :]

    try:
        fm1: dict[str, object] = yaml.safe_load(fm1_text) or {}
        fm2: dict[str, object] = yaml.safe_load(fm2_text) or {}
    except yaml.YAMLError:
        return content  # can't parse — leave unchanged to avoid data loss

    merged = {**fm1, **fm2}  # second block wins on collision
    # historic regression: preserve dict insertion order rather than sorting keys alphabetically.
    # yaml.dump(sort_keys=True) reordered keys on every collapse; use _render_raw_key_value
    # to emit each key in merged's iteration order (fm1 order, with fm2 additions appended).
    fm_lines: list[str] = []
    for k, v in merged.items():
        fm_lines.extend(render_raw_key_value(k, v))
    merged_yaml = "\n".join(fm_lines)
    collapsed_lines = ["---", *merged_yaml.splitlines(), "---", *body_lines]
    result = "\n".join(collapsed_lines)
    if content.endswith("\n"):
        result += "\n"
    return result


def _load_and_prepare_lines(pursuit_path: str) -> tuple[str, list[str]]:
    with Path(pursuit_path).open(encoding="utf-8") as fh:
        content = fh.read()

    # Heal double-frontmatter before further processing (historic regression)
    content = _collapse_double_frontmatter(content)

    # implementation change: warn on duplicate YAML keys so operators catch data-loss hazards
    # before they silently corrupt frontmatter on the next write.
    lines = content.splitlines(keepends=True)
    str_lines = [ln.rstrip("\n") for ln in lines]
    try:
        _fm_start, _fm_end = _find_frontmatter_bounds(str_lines)
        _fm_text = "\n".join(str_lines[_fm_start + 1 : _fm_end])
        dup_keys = _detect_duplicate_yaml_keys_fm(_fm_text)
    except ValueError:
        dup_keys = []
    if dup_keys:
        # implementation note: abort on duplicate keys rather than silently losing data.
        # A warning allowed corrupted frontmatter to proceed; raising here forces
        # the operator to fix the file before any write can occur.
        raise FieldkitError(
            f"Duplicate YAML key(s) in {Path(pursuit_path).name}: {dup_keys!r} — fix frontmatter before syncing"
        )
    if not str_lines or not re.fullmatch(r"-{3,}\s*", str_lines[0]):
        str_lines = ["---", "---", *str_lines]
    return content, str_lines


# ── Salesforce ────────────────────────────────────────────────────────────────


def _normalize_sf_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize JSON to a status=ok envelope, accepting two input formats.

    Format 1: SF API envelope {"status": "ok", ...}
    Format 2: Direct fm key-value {"sf_stage": "Discover", ...} (no status key)

    An empty dict {} is rejected — passing {} would silently clear all sf_ fields.
    """
    if not data:
        return {"status": "error", "message": "Empty payload {} would wipe all sf_ fields. Pass actual field values."}
    if "status" in data:
        return data
    fm_to_payload = dict({**SF_OPP_KEY_MAP, **SF_ACCOUNT_KEY_MAP}.items())
    synthetic: dict[str, Any] = {"status": "ok"}
    for k, v in data.items():
        synthetic[fm_to_payload.get(k, k)] = v
    return synthetic


# implementation change: monetary fields that may carry a dollar-string format (e.g. '$500,000').
# When the incoming value is numerically equal to the existing dollar-string, preserve
# the original format rather than coercing to a plain float.
_MONETARY_FM_KEYS: frozenset[str] = frozenset({"sf_acv", "sf_arr", "sf_consulting_acv", "sf_training_acv"})
_DOLLAR_STRING_RE = re.compile(r"^\$[\d,]+(\.\d+)?$")


def _preserve_monetary(new_val: Any, existing_str: str) -> str:
    """Return *existing_str* when it is a dollar-string and its numeric value matches *new_val*.

    Falls back to ``_val(new_val)`` when:
    - *existing_str* is not a dollar-string (e.g. plain float or empty)
    - *new_val* is not numeric
    - the numeric values differ (caller supplied a genuinely new value)

    This prevents a write round-trip from coercing ``'$365,049'`` → ``365049.0``.
    """
    if not existing_str or not _DOLLAR_STRING_RE.match(existing_str):
        return _val(new_val)
    try:
        existing_numeric = float(existing_str.lstrip("$").replace(",", ""))
    except ValueError:
        return _val(new_val)
    try:
        incoming_numeric = float(str(new_val).lstrip("$").replace(",", ""))
    except (ValueError, TypeError):
        return _val(new_val)
    if existing_numeric == incoming_numeric:
        return existing_str
    return _val(new_val)


def _scan_existing_monetary(
    fm_lines: list[str],
    monetary_keys_in_payload: set[str],
) -> dict[str, str]:
    """Scan frontmatter lines for existing monetary field values.

    Returns a dict mapping fm_key → raw string value (stripped of quotes).
    Only called when the payload contains at least one monetary field.
    """
    existing: dict[str, str] = {}
    for ln in fm_lines:
        for fm_key in monetary_keys_in_payload:
            if ln.startswith(f"{fm_key}:"):
                existing[fm_key] = ln.split(":", 1)[1].strip().strip('"').strip("'")
                break
    return existing


def _build_sf_values(
    data: dict[str, Any],
    key_map: dict[str, str],
    pulled_at: str,
    existing_monetary: dict[str, str],
) -> dict[str, str]:
    """Build the dict of fm_key → formatted string value for all keys in key_map."""
    sf_values: dict[str, str] = {}
    for fm_key, json_key in key_map.items():
        raw = data.get(json_key) if json_key != "pulled_at" else pulled_at
        if fm_key in _MONETARY_FM_KEYS:
            sf_values[fm_key] = _preserve_monetary(raw, existing_monetary.get(fm_key, ""))
        else:
            sf_values[fm_key] = _val(raw)
    return sf_values


def _append_deal_splits(fm_lines: list[str], data: dict[str, Any]) -> None:
    """Append deal_splits YAML block to fm_lines in place."""
    raw_splits = data.get("deal_splits")
    splits = raw_splits if isinstance(raw_splits, list) else []
    normalized = [_normalize_deal_split(entry, index) for index, entry in enumerate(splits)]
    fm_lines.extend(render_raw_key_value(_DEAL_SPLITS_KEY, normalized))


def _normalize_deal_split(entry: Any, index: int) -> dict[str, Any]:
    """Validate and normalize one Salesforce deal-split payload entry."""
    if not isinstance(entry, dict):
        raise FieldkitError(f"deal_splits[{index}] must be an object")
    offering = entry.get("offering", "")
    if not isinstance(offering, str):
        raise FieldkitError(f"deal_splits[{index}].offering must be a string")
    try:
        pct = float(entry.get("pct", 0.0))
    except (TypeError, ValueError) as exc:
        raise FieldkitError(f"deal_splits[{index}].pct must be numeric") from exc
    return {"offering": offering, "pct": pct}


def _build_updated_fm_lines(
    fm_lines: list[str],
    data: dict[str, Any],
    key_map: dict[str, str],
    keys_ordered: list[str],
) -> list[str]:
    """Return updated frontmatter lines with SF values written and deal_splits appended."""
    if not data:
        raise ValueError("_build_updated_fm_lines called with empty payload — would wipe all sf_ fields")
    pulled_at = data.get("pulled_at") or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # implementation change: build a lookup of existing monetary field values before stripping.
    monetary_keys_in_payload = {
        fm_key for fm_key, json_key in key_map.items() if fm_key in _MONETARY_FM_KEYS and data.get(json_key) is not None
    }
    existing_monetary = _scan_existing_monetary(fm_lines, monetary_keys_in_payload) if monetary_keys_in_payload else {}

    sf_values = _build_sf_values(data, key_map, pulled_at, existing_monetary)

    fm_lines = _strip_sf_keys(fm_lines)
    while fm_lines and fm_lines[-1].strip() == "":
        fm_lines.pop()
    for fm_key in keys_ordered:
        fm_lines.append(_yaml_line(fm_key, sf_values[fm_key]))
    if key_map is SF_OPP_KEY_MAP:
        _append_deal_splits(fm_lines, data)
    return fm_lines


def _inject_opportunity_id_from_file(data: dict[str, Any], existing_fm_lines: list[str]) -> dict[str, Any]:
    """historic regression: inject opportunity_id from file frontmatter when payload omits it.

    Agents often pass partial payloads (e.g. only sf_stage + sf_arr) without
    repeating the opportunity_id already in the file.  We use `not in data` rather
    than `not data.get(...)` to distinguish "key absent" from "key present with null
    value" — the latter is an intentional blank and must still trigger the historic regression guard.
    """
    if "opportunity_id" in data:
        return data
    for ln in existing_fm_lines:
        if ln.startswith("sf_opportunity_id:"):
            raw_file_opp = ln.split(":", 1)[1].strip()
            file_opp_id = _strip_yaml_inline_comment(raw_file_opp)
            if file_opp_id:
                if not _SF_OPP_ID_RE.fullmatch(file_opp_id):
                    logger.warning(
                        "[historic regression] sf_opportunity_id %r in file does not match "
                        "Salesforce ID format (15 or 18 alphanumeric chars) — "
                        "skipping injection; historic regression guard will abort the write.",
                        file_opp_id,
                    )
                else:
                    data = {**data, "opportunity_id": file_opp_id}
                    logger.debug(
                        "[historic regression] opportunity_id injected from file frontmatter: %r",
                        file_opp_id,
                    )
            break
    return data


def _clean_opp_payload(data: dict[str, Any]) -> dict[str, Any] | None:
    """historic regression: strip unrecognised keys and validate at least one data field is present.

    Returns cleaned dict, or None if no recognised SF data fields remain.
    """
    valid_payload_keys = set(SF_OPP_KEY_MAP.values()) | {"opportunity_id", "status", "pulled_at", "deal_splits"}
    cleaned: dict[str, Any] = {}
    for k, v in data.items():
        if k in valid_payload_keys:
            cleaned[k] = v
        else:
            logger.warning(
                "[historic regression] Dropping unrecognised SF payload key %r — not in SF_OPP_KEY_MAP",
                k,
            )
    _data_field_keys = set(SF_OPP_KEY_MAP.values()) - {"opportunity_id"}
    if not any(k in cleaned for k in _data_field_keys):
        click.echo("ERROR: no recognised SF fields in payload", err=True)
        return None
    return cleaned


def _check_opp_id_guard(data: dict[str, Any], existing_fm_lines: list[str]) -> bool:
    """historic regression: refuse to blank an existing sf_opportunity_id.

    Returns True if the write should proceed, False if it should be aborted.
    """
    existing_opp_id: str = ""
    for ln in existing_fm_lines:
        if ln.startswith("sf_opportunity_id:"):
            raw_opp = ln.split(":", 1)[1].strip()
            existing_opp_id = _strip_yaml_inline_comment(raw_opp)
            break
    incoming_opp_id = data.get("opportunity_id") or ""
    if existing_opp_id and not incoming_opp_id:
        logger.error(
            "[historic regression] Refusing to blank sf_opportunity_id: existing=%r, incoming=%r — "
            "write aborted. Pass a valid opportunity_id to update this field.",
            existing_opp_id,
            incoming_opp_id,
        )
        return False
    return True


def _validate_and_prepare_opp_payload(
    data: dict[str, Any],
    existing_fm_lines: list[str],
) -> dict[str, Any] | None:
    """Apply historic regression, historic regression, and historic regression guards for opportunity-mode writes.

    Returns the cleaned payload dict, or None if the write should be aborted.
    """
    data = _inject_opportunity_id_from_file(data, existing_fm_lines)
    cleaned = _clean_opp_payload(data)
    if cleaned is None:
        return None
    if not _check_opp_id_guard(cleaned, existing_fm_lines):
        return None
    return cleaned


def _warn_name_slug_divergence(data: dict[str, Any], pursuit_path: str) -> None:
    """implementation change: warn when sf_name slug diverges from the pursuit file stem."""
    opp_name = data.get("name", "")
    if opp_name:
        file_stem = Path(pursuit_path).stem
        slugified = re.sub(r"[^a-z0-9]+", "-", str(opp_name).lower()).strip("-")
        if slugified and slugified != file_stem:
            logger.warning(
                "sf_name slug %r diverges from pursuit file stem %r — "
                "consider renaming the file to match the SF opportunity name.",
                slugified,
                file_stem,
            )


_STAGE_KEY_RE = re.compile(r"^stage\s*:\s*(.+)$", re.MULTILINE)
_SF_STAGE_KEY_RE = re.compile(r"^sf_stage\s*:\s*(.+)$", re.MULTILINE)


def _warn_stage_drift(new_content: str, pursuit_path: str, quiet: bool = False) -> bool:
    """implementation note: emit a warning when local ``stage`` and SF ``sf_stage`` diverge.

    Both values are normalised (lower-case, spaces → hyphens) before comparison
    so minor formatting differences don't produce false positives.  Only the
    frontmatter block is scanned — body text is ignored.

    Args:
        new_content: Full file content after the atomic write.
        pursuit_path: String path to the pursuit file (used in log/echo messages).
        quiet: When True, suppress the stdout notice (JSON mode owns stdout).
            The ``logger.warning`` still fires.

    Returns:
        True when local and SF stages diverge, False otherwise.
    """
    # Restrict search to the frontmatter block to avoid matching body text.
    written_lines = new_content.splitlines()
    try:
        fm_start, fm_end = _find_frontmatter_bounds(written_lines)
    except ValueError:
        return False  # no frontmatter — nothing to compare
    fm_text = "\n".join(written_lines[fm_start + 1 : fm_end])

    stage_m = _STAGE_KEY_RE.search(fm_text)
    sf_stage_m = _SF_STAGE_KEY_RE.search(fm_text)
    if not stage_m or not sf_stage_m:
        return False  # one or both keys absent — nothing to compare

    def _norm(val: str) -> str:
        return val.strip().strip('"').strip("'").lower().replace(" ", "-")

    local_stage = _norm(stage_m.group(1))
    sf_stage = _norm(sf_stage_m.group(1))
    if local_stage == sf_stage:
        return False

    path = Path(pursuit_path)
    logger.warning(
        "Stage drift in %s: local=%r SF=%r. Run 'fieldkit pursuit advance' to align.",
        path.name,
        local_stage,
        sf_stage,
    )
    if not quiet:
        click.echo(
            f"⚠  Stage drift in {path.name}: local stage={local_stage!r}, SF stage={sf_stage!r}."
            " Run 'fieldkit pursuit advance' to align."
        )
    return True


def _warn_duplicate_keys_after_write(new_content: str, pursuit_path: str) -> None:
    """implementation change / historic regression: post-write duplicate-key warning."""
    _written_lines = new_content.splitlines()
    try:
        _w_start, _w_end = _find_frontmatter_bounds(_written_lines)
        _w_fm_text = "\n".join(_written_lines[_w_start + 1 : _w_end])
        dup_keys = _detect_duplicate_yaml_keys_fm(_w_fm_text)
    except ValueError:
        dup_keys = []
    if dup_keys:
        click.echo(
            f"WARNING [{pursuit_path}]: duplicate YAML keys detected after write: "
            f"{', '.join(dup_keys)} — this may cause silent data loss on next read.",
            err=True,
        )


def _emit_json(payload: dict[str, Any]) -> None:
    """Emit a frontmatter result document on stdout."""
    click.echo(json.dumps(payload, indent=2, default=str))


def _prepare_legacy_migration(
    original_content: str,
    updated_content: str,
    pursuit_path: str,
) -> tuple[dict[str, Any], str] | None:
    """Validate and return an in-memory former-MEDDPICC migration, when needed."""
    original = parse_frontmatter(original_content)
    if original is None or "meddpicc" not in original[0]:
        return None
    parsed = parse_frontmatter(updated_content)
    if parsed is None:
        raise FieldkitError(f"Could not parse assembled frontmatter for {pursuit_path}")
    updated_frontmatter, body = parsed
    canonicalize_legacy_meddpicc(updated_frontmatter)
    return updated_frontmatter, body


def _finish_sf_mode(
    path: Path,
    pursuit_path: str,
    content: str,
    new_content: str,
    expected_mtime: float,
    data: dict[str, Any],
    key_map: dict[str, str],
    keys_ordered: list[str],
    as_json: bool,
    dry_run: bool,
) -> None:
    """Preview or persist an assembled Salesforce frontmatter update."""
    legacy_migration = _prepare_legacy_migration(content, new_content, pursuit_path)
    is_opportunity = key_map is SF_OPP_KEY_MAP
    parsed = legacy_migration or parse_frontmatter(new_content)
    if parsed is None:
        raise FieldkitError(f"Could not parse assembled frontmatter for {pursuit_path}")
    updated_frontmatter, body = parsed
    original_lines = content.splitlines()
    create_frontmatter = not original_lines or re.fullmatch(r"-{3,}\s*", original_lines[0]) is None
    new_content = render_frontmatter_raw(
        path,
        updated_frontmatter,
        body,
        create=create_frontmatter,
        expected_mtime=expected_mtime,
    )
    if is_opportunity:
        errors = _validate_frontmatter_content(new_content, _get_schema_path())
        if errors:
            for error in errors:
                click.echo(f"SCHEMA ERROR: {error}", err=True)
            click.echo("ERROR: Canonical frontmatter validation failed — write aborted.", err=True)
            raise SystemExit(EXIT_PARTIAL)
    if dry_run:
        stage_drift = False
        if is_opportunity:
            _warn_name_slug_divergence(data, pursuit_path)
            stage_drift = _warn_stage_drift(new_content, pursuit_path, quiet=as_json)
        _warn_duplicate_keys_after_write(new_content, pursuit_path)
        if as_json:
            _emit_json(
                {
                    "mode": "sf",
                    "file": pursuit_path,
                    "written": False,
                    "dry_run": True,
                    "record_kind": "opportunity" if is_opportunity else "account",
                    "keys_previewed": keys_ordered,
                    "stage_drift": stage_drift,
                    "legacy_migration": legacy_migration is not None,
                }
            )
        else:
            migration_note = " (meddpicc -> legacy_meddpicc)" if legacy_migration is not None else ""
            click.echo(f"DRY RUN: would update Salesforce frontmatter: {pursuit_path}{migration_note}")
        return

    write_frontmatter_raw(
        path,
        updated_frontmatter,
        body,
        create=create_frontmatter,
        expected_mtime=expected_mtime,
    )
    click.echo(f"Frontmatter updated: {pursuit_path}", err=True)

    stage_drift = False
    if is_opportunity:
        _warn_name_slug_divergence(data, pursuit_path)
        stage_drift = _warn_stage_drift(new_content, pursuit_path, quiet=as_json)
    _warn_duplicate_keys_after_write(new_content, pursuit_path)
    if as_json:
        _emit_json(
            {
                "mode": "sf",
                "file": pursuit_path,
                "written": True,
                "record_kind": "opportunity" if is_opportunity else "account",
                "keys_written": keys_ordered,
                "stage_drift": stage_drift,
            }
        )


def _run_sf_mode(pursuit_path: str, json_string: str, as_json: bool = False, dry_run: bool = False) -> None:
    path = Path(pursuit_path)
    if not path.is_file():
        click.echo(f"ERROR: File not found: {pursuit_path}", err=True)
        raise SystemExit(EXIT_PARTIAL) from None
    expected_mtime = path.stat().st_mtime

    data = _load_sf_payload(json_string)

    key_map = SF_ACCOUNT_KEY_MAP if "account_id" in data else SF_OPP_KEY_MAP
    keys_ordered = list(key_map.keys())

    content, str_lines = _load_and_prepare_lines(pursuit_path)
    try:
        start, end = _find_frontmatter_bounds(str_lines)
    except ValueError as exc:
        click.echo(f"ERROR: {exc}", err=True)
        raise SystemExit(EXIT_PARTIAL) from None

    if key_map is SF_OPP_KEY_MAP:
        existing_fm_lines = str_lines[start + 1 : end]
        cleaned = _validate_and_prepare_opp_payload(data, existing_fm_lines)
        if cleaned is None:
            # Guards already explained the rejection on stderr; exit code stays 0.
            if as_json:
                result: dict[str, Any] = {
                    "mode": "sf",
                    "file": pursuit_path,
                    "written": False,
                    "reason": "payload rejected",
                }
                if dry_run:
                    result["dry_run"] = True
                _emit_json(result)
            elif dry_run:
                click.echo(f"DRY RUN: no write for {pursuit_path} — payload rejected")
            return
        data = cleaned

    fm_lines = _build_updated_fm_lines(str_lines[start + 1 : end], data, key_map, keys_ordered)
    new_content = "\n".join(str_lines[: start + 1] + fm_lines + str_lines[end:])
    if content.endswith("\n") and not new_content.endswith("\n"):
        new_content += "\n"

    if key_map is SF_OPP_KEY_MAP:
        schema_path = _get_schema_path()
        errors = _validate_frontmatter_content(new_content, schema_path)
        if errors:
            for err in errors:
                click.echo(f"SCHEMA ERROR: {err}", err=True)
            click.echo(f"ERROR: Frontmatter validation failed — write aborted. Schema: {schema_path}", err=True)
            raise SystemExit(EXIT_PARTIAL)

    # Guard: output must have exactly one frontmatter block (historic regression regression check)
    _assert_single_frontmatter(new_content, pursuit_path)

    _finish_sf_mode(
        path, pursuit_path, content, new_content, expected_mtime, data, key_map, keys_ordered, as_json, dry_run
    )


def _load_sf_payload(json_string: str) -> dict[str, Any]:
    """Parse and validate the object payload accepted by Salesforce mode."""
    try:
        raw_data = json.loads(json_string)
    except json.JSONDecodeError as exc:
        click.echo(f"ERROR: Invalid JSON: {exc}", err=True)
        raise SystemExit(EXIT_PARTIAL) from None
    if not isinstance(raw_data, dict):
        raise FieldkitError("Salesforce frontmatter payload must be a JSON object")
    data = _normalize_sf_payload(raw_data)
    if data.get("status") != "ok":
        raise FieldkitError("Empty SF payload {} would wipe all sf_ fields — aborting write")
    return data


# ── Quality check ────────────────────────────────────────────────────────────


def _load_fm_data(pursuit_path: str, fm_lines: list[str]) -> dict[str, Any]:
    """Load frontmatter as a dict for quality checks.

    Tries load_pursuit first, falls back to yaml.safe_load on failure.
    Returns {} on any error — never raises.
    """
    try:
        model, _, _ = load_pursuit(pursuit_path)
        return model.model_dump(mode="json")
    except Exception:  # noqa: BLE001
        logger.debug("load_pursuit failed for %s, trying YAML fallback", pursuit_path, exc_info=True)
    import yaml as _yaml

    try:
        fm_text = "\n".join(fm_lines)
        return _yaml.safe_load(fm_text) or {}
    except Exception:  # noqa: BLE001
        logger.debug("YAML fallback also failed for %s — leaving fm_data empty", pursuit_path, exc_info=True)
    return {}


def _count_backstory_refs(obj: Any, pursuit_path: str, path: str = "") -> int:
    """Recursively scan *obj* for Backstory-derived data and return advisory count."""
    count = 0
    if isinstance(obj, str):
        if "[backstory" in obj.lower():
            click.echo(
                f"ADVISORY [{pursuit_path}]: Backstory-derived data found in frontmatter field '{path}' — remove and replace with first-hand intelligence",
                err=True,
            )
            count += 1
    elif isinstance(obj, dict):
        for k, v in obj.items():
            count += _count_backstory_refs(v, pursuit_path, path=k if not path else f"{path}.{k}")
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            count += _count_backstory_refs(item, pursuit_path, path=f"{path}[{i}]")
    return count


def _quality_check_pursuit(pursuit_path: str, as_json: bool = False) -> None:
    content = _read_quality_check_content(pursuit_path)

    lines = content.splitlines()
    dashes = [i for i, ln in enumerate(lines) if re.fullmatch(r"-{3,}\s*", ln)]
    if len(dashes) < 2:
        if as_json:
            _emit_json({"mode": "quality-check", "file": pursuit_path, "has_frontmatter": False, "advisory_count": 0})
        return  # No frontmatter — nothing to check

    fm_lines = lines[dashes[0] + 1 : dashes[1]]
    fm_data = _load_fm_data(pursuit_path, fm_lines)

    # historic regression: track advisory/critical count so the final PASS line accurately reflects issues.
    # Advisory detail is rendered on stderr so it remains visible alongside
    # the JSON document on stdout.
    advisory_count = _count_backstory_refs(fm_data, pursuit_path)

    if as_json:
        _emit_json(
            {
                "mode": "quality-check",
                "file": pursuit_path,
                "has_frontmatter": True,
                "advisory_count": advisory_count,
            }
        )
        return

    if advisory_count:
        noun = "advisory" if advisory_count == 1 else "advisories"
        click.echo(f"PASS [{pursuit_path}]: quality check complete — {advisory_count} {noun} found")
    else:
        click.echo(f"PASS [{pursuit_path}]: quality check complete — no issues found")


def _read_quality_check_content(pursuit_path: str) -> str:
    """Read a quality-check target or emit its terminal read error."""
    if not Path(pursuit_path).is_file():
        click.echo(f"ERROR: File not found: {pursuit_path}", err=True)
        raise SystemExit(EXIT_PARTIAL) from None

    try:
        with Path(pursuit_path).open(encoding="utf-8") as fh:
            content = fh.read()
    except OSError as exc:
        click.echo(f"ERROR: Cannot read {pursuit_path}: {exc}", err=True)
        raise SystemExit(EXIT_PARTIAL) from None
    return content


# ── Click CLI ─────────────────────────────────────────────────────────────────


def _dispatch_quality_check(file_opt: str | None, as_json: bool = False) -> None:
    """Handle --quality-check dispatch."""
    if not file_opt:
        raise click.UsageError("--quality-check requires --file <file.md>")
    _quality_check_pursuit(file_opt, as_json=as_json)


def _dispatch_validate(file_opt: str | None, as_json: bool = False) -> None:
    """Handle --validate dispatch."""
    if not file_opt:
        raise click.UsageError("--validate requires --file <file.md>")
    pursuit_path = file_opt
    # Skip template files — they contain placeholder values that intentionally
    # fail schema validation (e.g. "TBD", empty required fields).
    # historic regression: also skip any file inside a .template/ directory, not just template.md.
    path_obj = Path(pursuit_path)
    if path_obj.name == "template.md" or ".template" in str(path_obj):
        click.echo(f"SKIP: {pursuit_path} (template file — not validated)", err=True)
        if as_json:
            _emit_json({"mode": "validate", "file": pursuit_path, "status": "skipped", "errors": []})
        return
    try:
        with Path(pursuit_path).open(encoding="utf-8") as fh:
            content = fh.read()
    except OSError as exc:
        click.echo(f"ERROR: Cannot read {pursuit_path}: {exc}", err=True)
        raise SystemExit(EXIT_PARTIAL) from None
    schema_path = _get_schema_path()
    errors = _validate_frontmatter_content(content, schema_path)
    if errors:
        click.echo(f"INVALID: {pursuit_path}", err=True)
        for err in errors:
            click.echo(f"  {err}", err=True)
        # The validation ran and denied — the caller wants the errors, so the
        # document is emitted alongside the unchanged exit code.
        if as_json:
            _emit_json({"mode": "validate", "file": pursuit_path, "status": "invalid", "errors": errors})
        raise SystemExit(EXIT_PARTIAL)
    if as_json:
        _emit_json({"mode": "validate", "file": pursuit_path, "status": "valid", "errors": []})
        return
    click.echo(f"VALID: {pursuit_path}")


@declare_write("workspace")
@click.command("frontmatter")
@click.argument("file", required=False)
@click.argument("json_string", required=False)
@click.option("--quality-check", is_flag=True, default=False, help="Run Backstory quality checks.")
@click.option("--validate", is_flag=True, default=False, help="Validate frontmatter against the pursuit schema.")
@click.option(
    "--file",
    "file_opt",
    default=None,
    metavar="FILE",
    help="Pursuit file path (required for --quality-check and --validate).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the result of the selected mode as JSON on stdout.",
)
@click.option("--dry-run", is_flag=True, default=False, help="Preview SF-mode changes without writing the file.")
def cli(
    file: str | None,
    json_string: str | None,
    quality_check: bool,
    validate: bool,
    file_opt: str | None,
    as_json: bool,
    dry_run: bool,
) -> None:
    """Write sf_* frontmatter fields to pursuit files.

    \b
    Modes:
      FILE JSON_STRING          SF-mode: upsert Salesforce fields from JSON
      --quality-check --file F  Backstory advisory check
      --validate --file F       Schema validation
    """
    _validate_mode_options(dry_run, quality_check, validate)

    if quality_check:
        _dispatch_quality_check(file_opt, as_json=as_json)
        return

    if validate:
        _dispatch_validate(file_opt, as_json=as_json)
        return

    # Default SF-mode: positional FILE JSON_STRING
    if not file or not json_string:
        raise click.UsageError("Provide FILE and JSON_STRING for SF-mode, or use --quality-check / --validate.")
    _run_sf_mode(file, json_string, as_json=as_json, dry_run=dry_run)


def _validate_mode_options(dry_run: bool, quality_check: bool, validate: bool) -> None:
    """Reject dry-run when the selected mode cannot write Salesforce data."""
    if dry_run and (quality_check or validate):
        raise click.UsageError("--dry-run is only valid for SF-mode FILE JSON_STRING")


# ── Legacy entry point ────────────────────────────────────────────────────────
