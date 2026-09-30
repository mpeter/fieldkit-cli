"""Retained successor contracts never supply their own approval authority."""

import json
import os
from collections.abc import Iterator
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

from scripts import export_public_tree, public_history_source, quality_source
from tests.test_public_history_source import _capture
from tests.test_public_history_source import history as history

pytestmark = pytest.mark.unit
History = tuple[Path, public_history_source.ApprovedCutoverAnchor, bytes]


@pytest.mark.parametrize("boundary", ["schema", "semantic", "canonical"])
def test_whole_source_decoder_sanitizes_recursion(
    history: History, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    payload = _bytes(_payload(history))

    def recurse(*args: object, **kwargs: object) -> None:
        raise RecursionError("private-recursion-sentinel")

    owner = export_public_tree if boundary == "schema" else public_history_source
    name = {"schema": "_validate_schema", "semantic": "_validate_source", "canonical": "_canonical_bytes"}[boundary]
    monkeypatch.setattr(owner, name, recurse)
    with pytest.raises(ValueError, match="public history source exceeds nesting limit") as caught:
        public_history_source.source_from_bytes(payload, expected_anchor=history[1], cutover_record=history[2])
    assert str(caught.value) == "public history source exceeds nesting limit"
    assert caught.value.__cause__ is None


def _bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")


def _payload(history: History) -> dict[str, Any]:
    value = json.loads(_bytes(asdict(_capture(history))))
    assert isinstance(value, dict)
    return value


def test_canonical_round_trip_and_current_snapshot_verification(history: History, tmp_path: Path) -> None:
    repo, anchor, record = history
    source = _capture(history)
    serialized = public_history_source.source_bytes(source, expected_anchor=anchor, cutover_record=record)
    assert serialized == _bytes(asdict(source))
    path = tmp_path / "source.json"
    path.write_bytes(serialized)
    loaded = public_history_source.load_source(path, expected_anchor=anchor, cutover_record=record)
    assert loaded == source
    assert public_history_source.source_from_bytes(serialized, expected_anchor=anchor, cutover_record=record) == source
    snapshot = tmp_path / "snapshot"
    assert (
        public_history_source.materialize_source(repo, loaded, snapshot, expected_anchor=anchor, cutover_record=record)
        == source
    )
    assert (
        public_history_source.verify_source(repo, loaded, snapshot, expected_anchor=anchor, cutover_record=record)
        == source
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("schema_version", 2),
        ("source_kind", "initial-export"),
        ("repository", "other/fieldkit-cli"),
        ("repository_id", 43),
        ("repository_id", True),
        ("repository_id", 42.0),
        ("version", "1.0.0"),
        ("version", "01.1.0"),
        ("version", "1.1.0rc1"),
        ("version", "9" * 33 + ".1.0"),
        ("planned_tag", "v1.2.0"),
        ("source_commit", "X" * 40),
        ("source_tree", "a" * 40),
        ("policy_path", "../policy.json"),
        ("policy_oid", "invalid"),
        ("policy_sha256", "invalid"),
        ("entries", []),
        ("entries", {}),
        ("unexpected", "unknown"),
    ],
)
def test_identity_mutations_fail_closed(history: History, field: str, value: object) -> None:
    _, anchor, record = history
    payload = _payload(history)
    payload[field] = value
    with pytest.raises(ValueError):
        public_history_source.source_from_bytes(_bytes(payload), expected_anchor=anchor, cutover_record=record)


@pytest.mark.parametrize(
    "field", ["repository", "repository_id", "initial_commit", "initial_tree", "record_sha256", "unknown"]
)
def test_retained_anchor_cannot_override_independent_anchor(history: History, field: str) -> None:
    _, anchor, record = history
    payload = _payload(history)
    payload["anchor"][field] = {
        "repository": "other/fieldkit-cli",
        "repository_id": 99,
        "initial_commit": "a" * 40,
        "initial_tree": "a" * 40,
        "record_sha256": "a" * 64,
        "unknown": "field",
    }[field]
    with pytest.raises(ValueError):
        public_history_source.source_from_bytes(_bytes(payload), expected_anchor=anchor, cutover_record=record)


def test_record_digest_and_trusted_anchor_are_required(history: History) -> None:
    _, anchor, record = history
    payload = _bytes(_payload(history))
    with pytest.raises(ValueError, match="digest"):
        public_history_source.source_from_bytes(payload, expected_anchor=anchor, cutover_record=record + b"\n")
    with pytest.raises(ValueError, match="anchor"):
        public_history_source.source_from_bytes(
            payload, expected_anchor=replace(anchor, initial_commit="f" * 40), cutover_record=record
        )


@pytest.mark.parametrize("field", ["anchor", "entries", "policy_sha256", "source_kind"])
def test_missing_fields_are_rejected(history: History, field: str) -> None:
    _, anchor, record = history
    payload = _payload(history)
    del payload[field]
    with pytest.raises(ValueError, match="schema"):
        public_history_source.source_from_bytes(_bytes(payload), expected_anchor=anchor, cutover_record=record)


@pytest.mark.parametrize(("field", "value"), [("category", "Bad Category"), ("rule_id", "x" * 129), ("mode", 100644)])
def test_entry_types_and_field_limits(history: History, field: str, value: object) -> None:
    _, anchor, record = history
    payload = _payload(history)
    payload["entries"][0][field] = value
    with pytest.raises(ValueError, match="schema"):
        public_history_source.source_from_bytes(_bytes(payload), expected_anchor=anchor, cutover_record=record)


def test_writer_rejects_inventory_before_dataclass_copy(history: History, monkeypatch: pytest.MonkeyPatch) -> None:
    _, anchor, record = history
    source = _capture(history)
    monkeypatch.setattr(quality_source, "MAX_ENTRY_COUNT", 2)

    def unexpected_copy(*args: object, **kwargs: object) -> object:
        pytest.fail("oversized inventory reached dataclass copy")

    monkeypatch.setattr(public_history_source, "asdict", unexpected_copy)
    with pytest.raises(ValueError, match="inventory exceeds size bound"):
        public_history_source.source_bytes(source, expected_anchor=anchor, cutover_record=record)


def test_writer_rejects_oversize_bytes(history: History, monkeypatch: pytest.MonkeyPatch) -> None:
    _, anchor, record = history
    source = _capture(history)
    monkeypatch.setattr(public_history_source, "MAX_SOURCE_BYTES", 16)
    with pytest.raises(ValueError, match="source exceeds size bound"):
        public_history_source.source_bytes(source, expected_anchor=anchor, cutover_record=record)


def test_writer_stops_encoding_at_byte_bound(history: History, monkeypatch: pytest.MonkeyPatch) -> None:
    _, anchor, record = history
    source = _capture(history)
    monkeypatch.setattr(public_history_source, "MAX_SOURCE_BYTES", 16)

    def fragments(*args: object, **kwargs: object) -> Iterator[str]:
        yield " " * 17
        pytest.fail("writer exhausted encoding beyond byte bound")

    monkeypatch.setattr(json.JSONEncoder, "iterencode", fragments)
    with pytest.raises(ValueError, match="source exceeds size bound"):
        public_history_source.source_bytes(source, expected_anchor=anchor, cutover_record=record)


def test_writer_checks_large_fields_before_copy_or_encoding(history: History, monkeypatch: pytest.MonkeyPatch) -> None:
    _, anchor, record = history
    source = replace(_capture(history), source_commit="x" * (quality_source.GIT_OUTPUT_LIMIT_BYTES + 1))

    def unexpected_copy(*args: object, **kwargs: object) -> object:
        pytest.fail("oversized identity reached dataclass copy")

    monkeypatch.setattr(public_history_source, "asdict", unexpected_copy)
    with pytest.raises(ValueError, match="field exceeds size bound"):
        public_history_source.source_bytes(source, expected_anchor=anchor, cutover_record=record)


def test_exact_serialized_byte_boundary(history: History, monkeypatch: pytest.MonkeyPatch) -> None:
    _, anchor, record = history
    source = _capture(history)
    expected = _bytes(asdict(source))
    monkeypatch.setattr(public_history_source, "MAX_SOURCE_BYTES", len(expected))
    assert public_history_source.source_bytes(source, expected_anchor=anchor, cutover_record=record) == expected
    assert public_history_source.source_from_bytes(expected, expected_anchor=anchor, cutover_record=record) == source
    monkeypatch.setattr(public_history_source, "MAX_SOURCE_BYTES", len(expected) - 1)
    with pytest.raises(ValueError, match="source exceeds size bound"):
        public_history_source.source_bytes(source, expected_anchor=anchor, cutover_record=record)


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "omitted",
        "mode",
        "executable",
        "oid",
        "traversal",
        "git",
        "trailing",
        "backslash",
        "unicode-bound",
        "depth",
        "order",
        "unknown",
        "collision",
    ],
)
def test_entry_mutations_fail_closed(history: History, mutation: str) -> None:
    _, anchor, record = history
    payload = _payload(history)
    entries = payload["entries"]
    if mutation == "duplicate":
        entries.append(entries[0])
    elif mutation == "omitted":
        entries.pop()
    elif mutation == "order":
        entries.reverse()
    elif mutation == "collision":
        entries[1]["path"] = entries[0]["path"] + "/child"
    elif mutation == "unknown":
        entries[0]["unknown"] = "field"
    elif mutation == "mode":
        entries[0]["mode"] = "120000"
    elif mutation == "executable":
        entries[0]["mode"] = "100755"
    elif mutation == "oid":
        entries[0]["oid"] = "f" * 40
    else:
        entries[0]["path"] = {
            "traversal": "../secret",
            "git": ".git/config",
            "trailing": "README.md/",
            "backslash": "src\\evil",
            "unicode-bound": "é" * 3000,
            "depth": "a/" * 129 + "leaf",
        }[mutation]
    with pytest.raises(ValueError):
        public_history_source.source_from_bytes(_bytes(payload), expected_anchor=anchor, cutover_record=record)


@pytest.mark.parametrize(
    "mutation", ["whitespace", "no-newline", "duplicate-top", "duplicate-nested", "nonfinite", "utf8"]
)
def test_only_duplicate_free_canonical_json_is_accepted(history: History, mutation: str) -> None:
    _, anchor, record = history
    payload = _bytes(_payload(history))
    if mutation == "whitespace":
        payload = b" " + payload
    elif mutation == "no-newline":
        payload = payload.rstrip(b"\n")
    elif mutation == "duplicate-top":
        payload = payload.replace(b'{"anchor":', b'{"schema_version":1,"anchor":', 1)
    elif mutation == "duplicate-nested":
        payload = payload.replace(b'{"initial_commit":', b'{"repository_id":42,"initial_commit":', 1)
    elif mutation == "nonfinite":
        payload = payload.replace(b'"schema_version":1', b'"schema_version":NaN')
    else:
        payload = b"\xff"
    with pytest.raises(ValueError):
        public_history_source.source_from_bytes(payload, expected_anchor=anchor, cutover_record=record)


def test_oversize_is_rejected_before_json_parsing(history: History, monkeypatch: pytest.MonkeyPatch) -> None:
    _, anchor, record = history

    def unexpected_parse(*args: object, **kwargs: object) -> object:
        pytest.fail("oversized source reached JSON parser")

    monkeypatch.setattr(export_public_tree, "_json_object", unexpected_parse)
    with pytest.raises(ValueError, match="size bound"):
        public_history_source.source_from_bytes(
            b" " * (quality_source.GIT_OUTPUT_LIMIT_BYTES + 1), expected_anchor=anchor, cutover_record=record
        )


@pytest.mark.parametrize("kind", ["symlink", "fifo", "oversize", "hardlink", "ancestor-symlink"])
def test_file_reader_is_bounded_and_nofollow(history: History, tmp_path: Path, kind: str) -> None:
    _, anchor, record = history
    path = tmp_path / "source.json"
    if kind in {"symlink", "hardlink"}:
        target = tmp_path / "real.json"
        target.write_bytes(_bytes(_payload(history)))
        if kind == "symlink":
            path.symlink_to(target)
        else:
            path.hardlink_to(target)
    elif kind == "ancestor-symlink":
        target = tmp_path / "real"
        target.mkdir()
        (target / "source.json").write_bytes(_bytes(_payload(history)))
        linked = tmp_path / "linked"
        linked.symlink_to(target, target_is_directory=True)
        path = linked / "source.json"
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        with path.open("wb") as stream:
            stream.truncate(quality_source.GIT_OUTPUT_LIMIT_BYTES + 1)
    with pytest.raises(ValueError):
        public_history_source.load_source(path, expected_anchor=anchor, cutover_record=record)


def test_schema_is_local_closed_and_matches_captured_contract(history: History) -> None:
    schema_path = Path(__file__).parent.parent / "docs/release-readiness/public-history-source.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    payload = _payload(history)
    assert validator.is_valid(payload)
    assert schema["properties"]["entries"]["maxItems"] == quality_source.MAX_ENTRY_COUNT
    assert schema["$defs"]["tree_entry"]["properties"]["path"]["maxLength"] == quality_source.MAX_PATH_BYTES
    payload["unknown"] = "field"
    with pytest.raises(ValidationError, match="Additional properties"):
        validator.validate(payload)


def test_offline_reader_does_not_authenticate_current_ancestry(history: History) -> None:
    _, anchor, record = history
    payload = _payload(history)
    payload["source_commit"] = "f" * 40
    retained = public_history_source.source_from_bytes(_bytes(payload), expected_anchor=anchor, cutover_record=record)
    assert retained.source_commit == "f" * 40
    with pytest.raises(ValueError):
        public_history_source.verify_source(
            history[0], retained, history[0].parent / "absent", expected_anchor=anchor, cutover_record=record
        )


def test_offline_classification_labels_require_current_policy_verification(history: History, tmp_path: Path) -> None:
    repo, anchor, record = history
    source = _capture(history)
    snapshot = tmp_path / "snapshot"
    assert (
        public_history_source.materialize_source(repo, source, snapshot, expected_anchor=anchor, cutover_record=record)
        == source
    )
    payload = _payload(history)
    payload["entries"][0]["category"] = "another_category"
    retained = public_history_source.source_from_bytes(_bytes(payload), expected_anchor=anchor, cutover_record=record)
    assert retained.entries[0].category == "another_category"
    with pytest.raises(ValueError, match="independently reconstructed"):
        public_history_source.verify_source(repo, retained, snapshot, expected_anchor=anchor, cutover_record=record)
