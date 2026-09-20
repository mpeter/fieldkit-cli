"""Unit tests for fieldkit.util.atomic — locked_json_update and atomic_yaml_write."""

import json
import threading
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from ruamel.yaml.representer import RepresenterError

from fieldkit.util.atomic import atomic_round_trip_yaml_update, atomic_yaml_write, locked_json_update

pytestmark = pytest.mark.unit


# ── TestLockedJsonUpdate (flattened) ────────────────────────────────────────


def test_locked_json_update_creates_file_when_absent(tmp_path: Path) -> None:
    """locked_json_update creates the target file if it doesn't exist."""
    path = tmp_path / "status.json"
    with locked_json_update(path) as data:
        data["key"] = "value"
    assert json.loads(path.read_text()) == {"key": "value"}


def test_locked_json_update_merges_existing_content(tmp_path: Path) -> None:
    """locked_json_update preserves existing keys while adding new ones."""
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"existing": "data"}))
    with locked_json_update(path) as data:
        data["new_key"] = "new_value"
    result = json.loads(path.read_text())
    assert result["existing"] == "data"
    assert result["new_key"] == "new_value"


def test_locked_json_update_concurrent_writes_preserve_all_keys(tmp_path: Path) -> None:
    """20 concurrent threads writing different keys all appear in the result."""
    path = tmp_path / "status.json"
    errors: list[Exception] = []

    def write_key(i: int) -> None:
        try:
            with locked_json_update(path) as data:
                data[f"watcher-{i}"] = {"outcome": "ok", "run": i}
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write_key, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"
    result = json.loads(path.read_text())
    for i in range(20):
        assert f"watcher-{i}" in result, f"watcher-{i} missing from result"


def test_locked_json_update_same_key_concurrent_writes_no_corruption(tmp_path: Path) -> None:
    """10 threads updating the same key produce valid JSON with one of the written values."""
    path = tmp_path / "status.json"

    def write_watcher(i: int) -> None:
        with locked_json_update(path) as data:
            data["morning-brief"] = {"run": i}

    threads = [threading.Thread(target=write_watcher, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Result must be valid JSON with the key present
    result = json.loads(path.read_text())
    assert "morning-brief" in result
    assert isinstance(result["morning-brief"]["run"], int)


def test_locked_json_update_exception_inside_context_aborts_write(tmp_path: Path) -> None:
    """If an exception is raised inside the context, the file is not written."""
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"original": "value"}))
    with pytest.raises(ValueError, match="intentional"), locked_json_update(path) as data:
        data["new"] = "value"
        raise ValueError("intentional")
    # File should still have the original content
    result = json.loads(path.read_text())
    assert result == {"original": "value"}
    # No .tmp leftover — the write was aborted before the temp file was created
    assert not path.with_suffix(".json.tmp").exists()


def test_locked_json_update_creates_parent_dirs(tmp_path: Path) -> None:
    """locked_json_update creates missing parent directories."""
    path = tmp_path / "nested" / "deep" / "status.json"
    with locked_json_update(path) as data:
        data["x"] = 1
    assert path.exists()


def test_locked_json_update_lock_sidecar_created(tmp_path: Path) -> None:
    """A .lock sidecar file is created alongside the target."""
    path = tmp_path / "status.json"
    with locked_json_update(path) as data:
        data["k"] = "v"
    assert path.with_suffix(".json.lock").exists()


def test_locked_json_update_does_not_follow_predictable_temp_symlink(tmp_path: Path) -> None:
    """A legacy predictable temp symlink cannot redirect manifest content."""
    path = tmp_path / "status.json"
    sentinel = tmp_path / "outside.txt"
    sentinel.write_text("keep", encoding="utf-8")
    path.with_suffix(".json.tmp").symlink_to(sentinel)

    with locked_json_update(path) as data:
        data["safe"] = True

    result = json.loads(path.read_text(encoding="utf-8"))
    assert result == {"safe": True}
    assert sentinel.read_text(encoding="utf-8") == "keep"


# ── TestAtomicYamlWrite (flattened) ─────────────────────────────────────────


def test_atomic_yaml_write_writes_valid_yaml(tmp_path: Path) -> None:
    """atomic_yaml_write produces a readable YAML file."""
    path = tmp_path / "config.yaml"
    atomic_yaml_write(path, {"key": "value", "nested": {"a": 1}})
    result = yaml.safe_load(path.read_text())
    assert result["key"] == "value"
    assert result["nested"]["a"] == 1


def test_atomic_round_trip_yaml_update_serialization_failure_preserves_original(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    original = "# keep\nvalue: 1\n"
    path.write_text(original, encoding="utf-8")

    def add_unsupported_value(data: MutableMapping[str, Any]) -> None:
        data["unsupported"] = object()

    with pytest.raises(RepresenterError, match="cannot represent an object"):
        atomic_round_trip_yaml_update(path, add_unsupported_value)

    assert path.read_text(encoding="utf-8") == original


def test_atomic_yaml_write_no_partial_read_possible(tmp_path: Path) -> None:
    """Write goes via temp file — no partial content visible to readers."""
    path = tmp_path / "config.yaml"
    atomic_yaml_write(path, {"pipeline": {"quota": {"target": 5000000, "period": "2026-H2"}}})
    # File must exist and be valid YAML immediately after write
    result = yaml.safe_load(path.read_text())
    assert result["pipeline"]["quota"]["target"] == 5000000


def test_atomic_yaml_write_no_tmp_file_leftover(tmp_path: Path) -> None:
    """After a successful write, the .tmp file is cleaned up."""
    path = tmp_path / "config.yaml"
    atomic_yaml_write(path, {"a": 1})
    tmp = path.with_suffix(".yaml.tmp")
    assert not tmp.exists()


def test_atomic_yaml_write_does_not_follow_predictable_temp_symlink(tmp_path: Path) -> None:
    """A legacy predictable temp symlink cannot redirect YAML content."""
    path = tmp_path / "config.yaml"
    sentinel = tmp_path / "outside.txt"
    sentinel.write_text("keep", encoding="utf-8")
    path.with_suffix(".yaml.tmp").symlink_to(sentinel)

    atomic_yaml_write(path, {"safe": True})

    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {"safe": True}
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_atomic_yaml_write_creates_parent_dirs(tmp_path: Path) -> None:
    """atomic_yaml_write creates missing parent directories."""
    path = tmp_path / "nested" / "config.yaml"
    atomic_yaml_write(path, {"x": 1})
    assert path.exists()


def test_atomic_yaml_write_two_sequential_writes_last_wins(tmp_path: Path) -> None:
    """Sequential writes — second write is what the file contains."""
    path = tmp_path / "config.yaml"
    atomic_yaml_write(path, {"v": 1})
    atomic_yaml_write(path, {"v": 2})
    result = yaml.safe_load(path.read_text())
    assert result["v"] == 2
