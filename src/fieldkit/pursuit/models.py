"""Canonical Pydantic v2 models for pursuit and account frontmatter.

Shared contract for all tools that read or write YAML frontmatter.
"""

import datetime as _dt
import warnings
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fieldkit.pursuit.enums import MEDDPICCElement, Stage
from fieldkit.pursuit.utils import _parse_monetary


class TransitionEntry(BaseModel):
    """One entry in the transition-history list.

    All fields are optional because historical entries may predate the schema
    and may omit fields that were added later.  The ``from_`` attribute uses
    the alias ``from`` to match the YAML key (``from`` is a Python keyword).

    ``date`` accepts both ISO date strings and ``datetime.date`` objects
    (yaml.safe_load returns the latter for bare date values like ``2026-01-01``).

    Schema A (legacy): ``{stage, date}`` — single-field stage name.
    Schema B (current): ``{from, to, date}`` — explicit from/to stage pair.
    The two schemas are mutually exclusive — mixing them raises ValueError.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    # Accept date objects from yaml.safe_load as well as plain strings.
    # Use _dt.date to avoid the field name "date" shadowing the type name.
    date: _dt.date | str | None = None
    from_: str | None = Field(None, alias="from")
    to: str | None = None
    # historic regression: Schema A entries use {stage, date} instead of {from, to, date}.
    # Adding this field prevents data loss when Schema A entries are round-tripped
    # through load_pursuit() + _render_transition_history().
    stage: str | None = None
    gate_result: str | None = Field(None, alias="gate-result")
    override_reason: str | None = Field(None, alias="override-reason")
    note: Any = None

    @model_validator(mode="before")
    @classmethod
    def _canonicalize_legacy_gate_provenance(cls, data: Any) -> Any:
        """Migrate the deployed ``gate-status``/``note`` history shape without data loss."""
        if not isinstance(data, dict):
            return data
        canonical = dict(data)
        legacy_result = canonical.pop("gate-status", None)
        current_result = canonical.get("gate-result")
        if legacy_result is not None:
            if current_result is not None and current_result != legacy_result:
                raise ValueError("transition history has conflicting gate-status and gate-result values")
            canonical["gate-result"] = legacy_result

        note = canonical.get("note")
        if canonical.get("gate-result") == "override" and isinstance(note, str) and note.startswith("Override: "):
            legacy_reason = note.removeprefix("Override: ")
            current_reason = canonical.get("override-reason")
            if current_reason is not None and current_reason != legacy_reason:
                raise ValueError("transition history has conflicting note and override-reason values")
            canonical["override-reason"] = legacy_reason
            canonical.pop("note")
        return canonical

    @model_validator(mode="after")
    def _check_schema_exclusivity(self) -> "TransitionEntry":
        """Enforce mutual exclusion: Schema A (stage=) and Schema B (from_=/to=) cannot coexist.

        Schema A uses ``stage`` to record the destination stage name.
        Schema B uses ``from_`` and/or ``to`` to record an explicit transition pair.
        Mixing the two schemas in a single entry is ambiguous and likely a data error.

        Raises:
            ValueError: When both ``stage`` and any of ``from_``/``to`` are set.
        """
        has_a = self.stage is not None
        has_b = self.from_ is not None or self.to is not None
        if has_a and has_b:
            raise ValueError(
                "TransitionEntry: Schema A (stage=) and Schema B (from_=/to=) are mutually exclusive. "
                "Use one schema per entry."
            )
        return self


class LegacyMEDDPICC(BaseModel):
    """Historical local MEDDPICC data retained without active score semantics.

    Element values intentionally remain unvalidated. Real legacy files include
    partial and malformed scorecards, and migration must preserve that evidence
    exactly rather than defaulting, clamping, or interpreting it.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow", frozen=True)

    schema_version: Literal[1]
    status: Literal["historical"]
    metrics: Any = None
    economic_buyer: Any = Field(None, alias=MEDDPICCElement.ECONOMIC_BUYER)
    decision_criteria: Any = Field(None, alias=MEDDPICCElement.DECISION_CRITERIA)
    decision_process: Any = Field(None, alias=MEDDPICCElement.DECISION_PROCESS)
    identify_pain: Any = Field(None, alias=MEDDPICCElement.IDENTIFY_PAIN)
    champion: Any = None
    competition: Any = None
    paper_process: Any = Field(None, alias=MEDDPICCElement.PAPER_PROCESS)
    composite: Any = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def _reject_nonnumeric_version_lookalikes(cls, value: Any) -> Any:
        """Match JSON Schema by rejecting booleans and numeric strings."""
        if isinstance(value, (bool, str)):
            raise ValueError("schema_version must be the number 1")
        return value


def canonicalize_legacy_meddpicc(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with the former ``meddpicc`` key migrated in memory.

    A null former or canonical value represents absent historical evidence. It
    must remain readable without fabricating a versioned historical scorecard.
    """
    has_old = "meddpicc" in data
    has_canonical = "legacy_meddpicc" in data
    if has_old and has_canonical:
        raise ValueError("frontmatter contains both meddpicc and legacy_meddpicc")

    migrated: dict[str, Any] = {}
    for key, value in data.items():
        if key != "meddpicc":
            migrated[key] = value
            continue
        if value is None:
            continue
        if not isinstance(value, dict):
            raise ValueError("meddpicc must be a mapping to preserve as historical data")
        if "schema_version" in value or "status" in value:
            raise ValueError("former meddpicc data cannot contain reserved schema_version or status keys")
        legacy_value: dict[str, Any] = {"schema_version": 1, "status": "historical", **value}
        LegacyMEDDPICC.model_validate(legacy_value)
        migrated["legacy_meddpicc"] = legacy_value

    if has_canonical:
        canonical_value = migrated["legacy_meddpicc"]
        if canonical_value is None:
            migrated.pop("legacy_meddpicc")
            return migrated
        if not isinstance(canonical_value, dict):
            raise ValueError("legacy_meddpicc must be a mapping")
        LegacyMEDDPICC.model_validate(canonical_value)
    return migrated


class PursuitFrontmatter(BaseModel):
    """Frontmatter schema for pursuit .md files."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    stage: Stage
    gate_status: str | None = Field(None, alias="gate-status")
    last_transition: date | str | None = Field(None, alias="last-transition")
    last_updated: date | str | None = Field(None, alias="last-updated")
    transition_history: list[TransitionEntry] | None = Field(None, alias="transition-history")
    legacy_meddpicc: LegacyMEDDPICC | None = None

    # Salesforce fields — concrete types; validators run before coercion (mode='before')
    # so string inputs from YAML (e.g. "$1,234.56", ISO date strings) are normalised first.
    sf_opportunity_id: str | None = None
    sf_name: str | None = None
    sf_stage: str | None = None
    sf_close_date: str | None = None
    sf_arr: float | None = None
    sf_owner: str | None = None
    sf_next_steps: str | None = None
    sf_last_pulled: str | None = None
    sf_acv: float | None = None
    sf_consulting_acv: float | None = None
    sf_training_acv: float | None = None
    sf_deal_splits: list[dict[str, Any]] | None = None
    sf_probability: float | None = None
    sf_opportunity_number: str | None = None
    # historic regression: derived contract type — "fixed_price" | "standard". Absent (None)
    # is treated as "standard" by consumers, so unsynced files are unaffected.
    sf_contract_type: str | None = None

    @model_validator(mode="before")
    @classmethod
    def coerce_yaml_types(cls, data: Any) -> Any:
        """Handle yaml.safe_load type quirks."""
        if not isinstance(data, dict):
            return data
        data = canonicalize_legacy_meddpicc(data)
        # sf_last_pulled may arrive as datetime.datetime from yaml.safe_load
        val = data.get("sf_last_pulled")
        if isinstance(val, datetime):
            data["sf_last_pulled"] = val.isoformat()
        # sf_close_date may arrive as datetime.date from yaml.safe_load
        val = data.get("sf_close_date")
        if isinstance(val, date) and not isinstance(val, datetime):
            data["sf_close_date"] = val.isoformat()
        # sf_opportunity_number is a digit-only string; an unquoted scalar in a
        # hand-edited or legacy file loads as int. Coerce back to str so the
        # str|None field validates. (bool is an int subclass — exclude it.)
        val = data.get("sf_opportunity_number")
        if isinstance(val, int) and not isinstance(val, bool):
            data["sf_opportunity_number"] = str(val)
        return data

    @field_validator("sf_arr", "sf_acv", "sf_consulting_acv", "sf_training_acv", "sf_probability", mode="before")
    @classmethod
    def normalize_monetary(cls, v: Any, info: Any = None) -> float | None:
        """Normalize monetary fields to float on read where possible.

        Accepts "$500,000", 500000.0, 500000, None, or "".
        Normalizes to float so callers receive a consistent numeric type.
        Display formatting (e.g. "$500,000") is applied at render time only.

        Non-numeric strings (e.g. placeholders, legacy free text) are coerced
        to None — the field type is float | None and cannot store raw strings.
        Emits a UserWarning (instead of silently discarding) so callers can
        detect data loss before it propagates to the next SF sync write.

        Runs mode='before' so string inputs from YAML (e.g. "$1,234.56") are
        parsed to float before Pydantic applies the float | None type coercion.

        Args:
            v: Raw value from YAML or dict input.
            info: Pydantic FieldValidationInfo (provides field_name).

        Returns:
            Parsed float, or None when the value is absent or unparseable.
        """
        result = _parse_monetary(v)
        # _parse_monetary returns the original string for non-numeric inputs.
        # Since the field type is float | None, coerce non-numeric strings to None
        # rather than letting Pydantic raise a ValidationError.
        if isinstance(result, str):
            field_name = info.field_name if info is not None and hasattr(info, "field_name") else "unknown"
            warnings.warn(
                f"normalize_monetary: could not parse {field_name!r} value {v!r} — setting to None",
                UserWarning,
                stacklevel=2,
            )
            return None
        return result

    @field_validator("sf_deal_splits", mode="before")
    @classmethod
    def coerce_deal_splits(cls, v: Any) -> list[dict[str, Any]] | None:
        """Coerce non-list values (e.g. empty string from io.py None→'' conversion) to None."""
        if v is None or v == "":
            return None
        if isinstance(v, list):
            return v
        # Any other non-list type: silently discard rather than raise ValidationError.
        return None


SF_FIELD_NAMES: frozenset[str] = frozenset(k for k in PursuitFrontmatter.model_fields if k.startswith("sf_"))


class AccountFrontmatter(BaseModel):
    """Minimal frontmatter schema for account.md files."""

    model_config = ConfigDict(extra="ignore")

    name: str
    domains: list[str] | None = None
    people_ai_id: str | None = None
