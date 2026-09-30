"""Source-specific paths are bounded, deterministic, and independent of I/O."""

import hashlib
import os
from pathlib import Path

import pytest

from fieldkit.ingest.paths import compute_vault_path
from fieldkit.util.workspace_paths import resolve_workspace_output, validate_relative_output_path

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "relative",
    ["", "/note.md", "../note.md", "a/../note.md", "a//note.md", "./note.md", "a\\note.md", "C:note.md", "bad\x00.md"],
)
def test_workspace_output_refuses_ambiguous_relative_path(tmp_path: Path, relative: str) -> None:
    with pytest.raises(ValueError, match="Invalid relative output path"):
        resolve_workspace_output(tmp_path, relative)
    assert list(tmp_path.iterdir()) == []


def test_workspace_alias_resolves_without_creating_output(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    result = resolve_workspace_output(alias, "accounts/acme/meetings/note.md")
    assert result == workspace / "accounts/acme/meetings/note.md"
    assert list(workspace.iterdir()) == []


def test_relative_output_validator_returns_exact_valid_path() -> None:
    result = validate_relative_output_path("accounts/acme/meetings/note.md")

    assert result == "accounts/acme/meetings/note.md"


def test_workspace_output_requires_existing_root(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(ValueError, match="Cannot resolve prepared output path"):
        resolve_workspace_output(missing, "accounts/acme/meetings/note.md")

    assert not missing.exists()


@pytest.mark.parametrize("root_kind", ["file", "fifo"])
def test_workspace_output_requires_directory_root(tmp_path: Path, root_kind: str) -> None:
    workspace = tmp_path / "workspace"
    if root_kind == "file":
        workspace.write_text("not a directory", encoding="utf-8")
    else:
        os.mkfifo(workspace)

    with pytest.raises(ValueError, match="Cannot resolve prepared output path"):
        resolve_workspace_output(workspace, "accounts/acme/meetings/note.md")

    assert workspace.exists()


def test_workspace_output_rejects_child_redirect(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "redirect").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="Prepared output escapes workspace"):
        resolve_workspace_output(workspace, "redirect/note.md")

    assert list(outside.iterdir()) == []


def test_ingest_paths_does_not_reexport_generic_workspace_helpers() -> None:
    from fieldkit.ingest import paths

    assert not hasattr(paths, "resolve_workspace_output")
    assert not hasattr(paths, "validate_relative_output_path")


@pytest.mark.parametrize("title", ["Meeting", "", "❄", "x" * 8192])
def test_source_path_has_exact_digest_and_bounded_filename(tmp_path: Path, title: str) -> None:
    result = compute_vault_path(tmp_path, "acme", "2026-09-27", title, source_id="ambient:source-1")
    digest = hashlib.sha256(b"ambient:source-1").hexdigest()
    assert result.parent == tmp_path / "accounts/acme/meetings"
    assert result.name.startswith("2026-09-27-")
    assert result.name.endswith(f"-{digest}.md")
    assert len(result.name.encode("utf-8")) <= 179
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("account", ["", ".", "..", "../other", "a/b", "a\\b", "a" * 256])
def test_source_path_rejects_invalid_accounts(tmp_path: Path, account: str) -> None:
    with pytest.raises(ValueError, match="Invalid meeting account"):
        compute_vault_path(tmp_path, account, "2026-09-27", "Meeting", source_id="source-1")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("meeting_date", ["2026-02-30", "20260927", "../other"])
def test_source_path_rejects_invalid_dates(tmp_path: Path, meeting_date: str) -> None:
    with pytest.raises(ValueError, match="Invalid meeting date"):
        compute_vault_path(tmp_path, "acme", meeting_date, "Meeting", source_id="source-1")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("source_id", ["", "s" * 1025, "\ud800"])
def test_source_path_rejects_invalid_identity(tmp_path: Path, source_id: str) -> None:
    with pytest.raises(ValueError, match="Invalid meeting source identity"):
        compute_vault_path(tmp_path, "acme", "2026-09-27", "Meeting", source_id=source_id)
    assert list(tmp_path.iterdir()) == []


def test_source_path_rejects_overlong_title(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Meeting title exceeds its bound"):
        compute_vault_path(tmp_path, "acme", "2026-09-27", "x" * 8193, source_id="source-1")
    assert list(tmp_path.iterdir()) == []
