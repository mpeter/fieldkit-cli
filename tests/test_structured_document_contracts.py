"""Structured ownership checks select the full public policy and call real validators."""

import json
from pathlib import Path

import pytest

from fieldkit.util.bounded_process import BoundedProcessError, ProcessFailureReason
from scripts import check_structured_document_contracts as contracts

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("reason", ["timeout", "overflow"])
def test_expected_inventory_process_failures_are_fixed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reason: ProcessFailureReason,
) -> None:
    error = BoundedProcessError("fictional private process detail", reason=reason)

    def fail_inventory(_repo: Path) -> tuple[str, ...]:
        raise error

    monkeypatch.setattr(contracts, "_inventory", fail_inventory)
    result = contracts.main(["--repo", str(tmp_path)])
    assert result == 1
    assert capsys.readouterr().out == "Structured document contracts: FAIL\n"


@pytest.mark.parametrize("exception", [RuntimeError, KeyboardInterrupt])
def test_unexpected_inventory_exceptions_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exception: type[BaseException]
) -> None:
    error = exception("fixture interruption")

    def fail_inventory(_repo: Path) -> tuple[str, ...]:
        raise error

    monkeypatch.setattr(contracts, "_inventory", fail_inventory)
    with pytest.raises(exception) as raised:
        contracts.main(["--repo", str(tmp_path)])
    assert raised.value is error


def write_json(root: Path, path: str, value: object) -> None:
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value), encoding="utf-8")


def configure(root: Path, monkeypatch: pytest.MonkeyPatch, owners: tuple[contracts.Owner, ...]) -> None:
    write_json(
        root,
        "docs/release-readiness/public-tree-policy.json",
        {
            "schema_version": 1,
            "expected_repository": "mpeter/fieldkit-cli",
            "planned_tag": "v1.0.0",
            "rules": [
                {
                    "id": "include-fixture",
                    "action": "include",
                    "category": "documentation",
                    "patterns": ["docs/fixture/**", "docs/release-readiness/artifact-policy.json"],
                    "rationale": "Fictional test documents.",
                }
            ],
        },
    )
    monkeypatch.setattr(contracts, "OWNERS", owners)
    monkeypatch.setattr(contracts, "_inventory", lambda _repo: tuple(owner.path for owner in owners))


def schema(name: str) -> dict[str, object]:
    return {"$schema": contracts.SCHEMA_DIALECT, "$id": contracts.SCHEMA_BASE + name, "type": "object"}


def test_registered_paths_have_one_owner_each() -> None:
    paths = tuple(owner.path for owner in contracts.OWNERS)
    assert len(paths) == 27
    assert len(set(paths)) == 27
    assert sum(owner.kind == "schema" for owner in contracts.OWNERS) == 16


@pytest.mark.parametrize(
    "case", ["unknown", "missing", "missing-file", "duplicate", "unclassified", "ambiguous-policy"]
)
def test_inventory_and_ownership_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    owner = contracts.Owner("docs/fixture/fixture.schema.json", "schema")
    configure(tmp_path, monkeypatch, (owner,))
    write_json(tmp_path, owner.path, schema("fixture.schema.json"))
    if case == "unknown":
        monkeypatch.setattr(contracts, "_inventory", lambda _repo: (owner.path, "docs/fixture/unknown.json"))
    elif case == "missing":
        monkeypatch.setattr(contracts, "_inventory", lambda _repo: ())
    elif case == "missing-file":
        (tmp_path / owner.path).unlink()
    elif case == "duplicate":
        monkeypatch.setattr(contracts, "OWNERS", (owner, owner))
    elif case == "unclassified":
        monkeypatch.setattr(contracts, "_inventory", lambda _repo: (owner.path, "docs/unclassified.json"))
    else:
        policy_path = tmp_path / "docs/release-readiness/public-tree-policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["rules"].append({**policy["rules"][0], "id": "other-fixture"})
        write_json(tmp_path, policy_path.relative_to(tmp_path).as_posix(), policy)
    with pytest.raises(ValueError, match=r"ownership|unclassified|ambiguous|unavailable"):
        contracts.check(tmp_path)


def test_valid_local_reference_and_data_reference_are_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owners = (
        contracts.Owner("docs/fixture/one.schema.json", "schema"),
        contracts.Owner("docs/fixture/two.schema.json", "schema"),
    )
    configure(tmp_path, monkeypatch, owners)
    first = schema("one.schema.json")
    first["properties"] = {"value": {"$ref": "two.schema.json#/$defs/value"}}
    first["const"] = {"$ref": "https://example.com/ordinary-json-data"}
    second = schema("two.schema.json")
    second["$defs"] = {"value": {"type": "string"}}
    write_json(tmp_path, owners[0].path, first)
    write_json(tmp_path, owners[1].path, second)
    result = contracts.check(tmp_path)
    assert result == tuple(owner.path for owner in owners)


@pytest.mark.parametrize(
    "change",
    ["dialect", "id", "shape", "remote", "missing-fragment", "dynamic", "nested-id", "nested-dialect", "unused-ref"],
)
def test_invalid_schema_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    owner = contracts.Owner("docs/fixture/fixture.schema.json", "schema")
    configure(tmp_path, monkeypatch, (owner,))
    document = schema("fixture.schema.json")
    if change == "dialect":
        document.pop("$schema")
    elif change == "id":
        document["$id"] = "https://example.com/other.schema.json"
    elif change == "shape":
        document["type"] = "unsupported-type"
    elif change == "remote":
        document["$ref"] = "https://example.com/unregistered.schema.json"
    elif change == "missing-fragment":
        document["$ref"] = "#/$defs/missing"
    elif change == "dynamic":
        document["$dynamicRef"] = "#value"
    elif change == "nested-id":
        document["$defs"] = {"unused": {"$id": "nested-resource"}}
    elif change == "nested-dialect":
        document["$defs"] = {"unused": {"$schema": "https://example.com/unknown-dialect"}}
    else:
        document["$defs"] = {"unused": {"$ref": "#/$defs/missing"}}
    write_json(tmp_path, owner.path, document)
    with pytest.raises(ValueError, match="schema"):
        contracts.check(tmp_path)


def test_invalid_artifact_policy_uses_canonical_validator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = contracts.Owner("docs/release-readiness/artifact-policy.json", "artifact")
    configure(tmp_path, monkeypatch, (owner,))
    write_json(tmp_path, owner.path, {"schema_version": 1, "required_families": []})
    with pytest.raises(ValueError, match="required_families"):
        contracts.check(tmp_path)


@pytest.mark.parametrize("data", [b'{"duplicate":1,"duplicate":2}', b'{"x":1e999}', b"[]", b"\xff"])
def test_strict_json_rejected(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "fixture.json"
    path.write_bytes(data)
    with pytest.raises(ValueError, match=r"JSON|object|nonfinite"):
        contracts._object(tmp_path, path.name)


def test_acquired_bytes_are_bounded_before_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(contracts, "read_root_bytes", lambda _root, _path: b" " * (contracts.MAX_DOCUMENT_BYTES + 1))
    with pytest.raises(ValueError, match="byte limit"):
        contracts._object(tmp_path, "fixture.json")


def test_inventory_uses_full_git_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def git(repo: Path, *args: str) -> bytes:
        assert repo == tmp_path
        assert args == ("ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "docs")
        return b"docs/untracked.yaml\0docs/tracked.json\0docs/tracked.json\0docs/prose.md\0docs/config.toml\0"

    monkeypatch.setattr(contracts.quality_source, "_git", git)
    result = contracts._inventory(tmp_path)
    assert result == ("docs/config.toml", "docs/tracked.json", "docs/untracked.yaml")
