"""Safe schema-reference collection for explicitly supplied Salesforce records."""

import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from fieldkit.sf.client import SFAPIError, SFDataAccessError

PopulationState = Literal["observed-populated", "observed-null", "not-sampled"]

_SOBJECT_API_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?$")
_MAX_QUERY_FIELDS_PER_REQUEST = 50
MAX_SAMPLE_RECORDS = 10


class SchemaClient(Protocol):
    """The read-only SF operations needed to construct a schema reference."""

    def describe_sobject(self, sobject_type: str) -> dict[str, object]:
        """Return Salesforce describe metadata for an sObject."""
        ...

    def fetch_sobject(self, sobject_type: str, record_id: str, fields: str) -> dict[str, object]:
        """Fetch an explicitly supplied sObject record."""
        ...


@dataclass(frozen=True)
class SchemaField:
    """Safe describe metadata and aggregate population state for one field."""

    name: str
    label: str
    field_type: str
    population: PopulationState


@dataclass(frozen=True)
class SchemaReference:
    """A value-free schema reference built from explicitly supplied records."""

    sobject_type: str
    sample_count: int
    fields: tuple[SchemaField, ...]


def is_valid_sobject_api_name(value: str) -> bool:
    """Return whether ``value`` is a safe Salesforce object API name."""
    return _SOBJECT_API_NAME_RE.fullmatch(value) is not None


def is_valid_record_id(value: str) -> bool:
    """Return whether ``value`` is a 15- or 18-character Salesforce record ID."""
    return _RECORD_ID_RE.fullmatch(value) is not None


def classify_observed_population(
    field_names: Iterable[str], observations: Iterable[Mapping[str, object]]
) -> dict[str, PopulationState]:
    """Classify fields from presence and nullness without preserving record values."""
    not_sampled: PopulationState = "not-sampled"
    states: dict[str, PopulationState] = dict.fromkeys(field_names, not_sampled)
    for observation in observations:
        for field_name in states:
            if field_name not in observation or states[field_name] == "observed-populated":
                continue
            states[field_name] = "observed-null" if observation[field_name] is None else "observed-populated"
    return states


def collect_schema_reference(
    client: SchemaClient,
    sobject_type: str,
    record_ids: tuple[str, ...],
) -> SchemaReference:
    """Fetch safe metadata and aggregate states for explicitly supplied record IDs."""
    if len(record_ids) > MAX_SAMPLE_RECORDS:
        raise ValueError(f"At most {MAX_SAMPLE_RECORDS} record IDs may be sampled.")
    describe = client.describe_sobject(sobject_type)
    fields = _eligible_fields(describe)
    field_names = tuple(field.name for field in fields)
    populations = classify_observed_population(
        field_names, _record_observations(client, sobject_type, record_ids, field_names)
    )
    return SchemaReference(
        sobject_type=sobject_type,
        sample_count=len(record_ids),
        fields=tuple(
            SchemaField(
                name=field.name,
                label=field.label,
                field_type=field.field_type,
                population=populations[field.name],
            )
            for field in fields
        ),
    )


def _eligible_fields(describe: Mapping[str, object]) -> tuple[SchemaField, ...]:
    """Return described fields that have a name and are not deprecated and hidden.

    ``queryable`` is an object-level describe attribute, so eligibility must not read it per field.
    """
    raw_fields = describe.get("fields")
    if not isinstance(raw_fields, list):
        raise SFAPIError("SF describe response did not contain a fields list.")

    fields: list[SchemaField] = []
    for raw_field in raw_fields:
        if not isinstance(raw_field, Mapping) or raw_field.get("deprecatedAndHidden") is True:
            continue
        name = raw_field.get("name")
        if not isinstance(name, str):
            continue
        label = raw_field.get("label")
        field_type = raw_field.get("type")
        fields.append(
            SchemaField(
                name=name,
                label=label if isinstance(label, str) else name,
                field_type=field_type if isinstance(field_type, str) else "unknown",
                population="not-sampled",
            )
        )
    if not fields:
        raise SFDataAccessError("SF describe response contained no eligible fields.")
    return tuple(fields)


def _record_observations(
    client: SchemaClient,
    sobject_type: str,
    record_ids: tuple[str, ...],
    field_names: tuple[str, ...],
) -> Iterator[Mapping[str, object]]:
    for record_id in record_ids:
        for field_chunk in _chunked(field_names, _MAX_QUERY_FIELDS_PER_REQUEST):
            yield client.fetch_sobject(sobject_type, record_id, ",".join(field_chunk))


def _chunked(values: tuple[str, ...], chunk_size: int) -> Iterator[tuple[str, ...]]:
    for start in range(0, len(values), chunk_size):
        yield values[start : start + chunk_size]
