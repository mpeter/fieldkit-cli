"""Strict parsed identity discovery replaces permissive extraction fallbacks."""

from pathlib import Path

import pytest

from fieldkit.errors import FieldkitError, SalesforceSyncPartialError
from fieldkit.sf.sync import local_pursuits, match_pursuit, read_local_pursuit
from tests.test_sf_sync_domain import OPPORTUNITY_ID, pursuit

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("identity", ["006000000000AAA", "006000000000AAAabc"])
def test_valid_parsed_identity(tmp_path: Path, identity: str) -> None:
    path = pursuit(tmp_path, identity=identity)
    result = read_local_pursuit(path, workspace=tmp_path)
    assert result.opportunity_id == identity
    assert match_pursuit(path.parent, identity, workspace=tmp_path) == path


@pytest.mark.parametrize("value", ["null", '""'])
def test_absent_identity_is_untracked(tmp_path: Path, value: str) -> None:
    path = pursuit(tmp_path, identity=value)
    assert read_local_pursuit(path, workspace=tmp_path).opportunity_id is None
    assert match_pursuit(path.parent, OPPORTUNITY_ID, workspace=tmp_path) is None


@pytest.mark.parametrize(
    "metadata",
    [
        "sf_opportunity_id: TBD",
        "sf_opportunity_id: NEEDS-LOOKUP",
        "sf_opportunity_id: 001000000000AAA",
        "sf_opportunity_id: 123",
        "sf_opportunity_id: [006000000000AAA]",
        "sf_opportunity_id: 006000000000AAA\nsf_opportunity_id: 006000000000AAB",
        "sf_opportunity_id: key: value",
        "- 006000000000AAA",
    ],
)
def test_invalid_or_ambiguous_metadata_cannot_be_silently_skipped(tmp_path: Path, metadata: str) -> None:
    path = pursuit(tmp_path)
    path.write_text(f"---\n{metadata}\n---\n# Body\n", encoding="utf-8")
    with pytest.raises(FieldkitError, match=r"identity|frontmatter"):
        local_pursuits(path.parent, workspace=tmp_path)


def test_body_only_or_legacy_key_never_matches(tmp_path: Path) -> None:
    path = pursuit(tmp_path)
    path.write_text(f"---\nsf-opportunity-id: {OPPORTUNITY_ID}\n---\n{OPPORTUNITY_ID}\n", encoding="utf-8")
    assert match_pursuit(path.parent, OPPORTUNITY_ID, workspace=tmp_path) is None


def test_duplicate_identity_is_not_first_match_wins(tmp_path: Path) -> None:
    path = pursuit(tmp_path, identity=OPPORTUNITY_ID)
    path.with_name("duplicate.md").write_bytes(path.read_bytes())
    with pytest.raises(FieldkitError, match="multiple pursuits"):
        match_pursuit(path.parent, OPPORTUNITY_ID, workspace=tmp_path)


def test_symlinked_pursuit_cannot_be_read(tmp_path: Path) -> None:
    path = pursuit(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(FieldkitError, match="approved workspace"):
        local_pursuits(path.parent, workspace=tmp_path)


def test_missing_directory_is_a_complete_untracked_result(tmp_path: Path) -> None:
    assert match_pursuit(tmp_path / "accounts" / "acme-corp" / "pursuits", OPPORTUNITY_ID, workspace=tmp_path) is None


def test_non_pursuit_files_are_not_scanned(tmp_path: Path) -> None:
    path = pursuit(tmp_path, identity=OPPORTUNITY_ID)
    for name in ("template.md", "gmail-intel.md"):
        path.with_name(name).write_text("invalid metadata", encoding="utf-8")
    assert local_pursuits(path.parent, workspace=tmp_path)[0].path == path


def test_workspace_alias_keeps_child_namespace_confined(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = pursuit(workspace, identity=OPPORTUNITY_ID)
    alias = tmp_path / "workspace-alias"
    alias.symlink_to(workspace, target_is_directory=True)
    assert match_pursuit(alias / "accounts" / "acme-corp" / "pursuits", OPPORTUNITY_ID, workspace=alias) == path


def test_directory_scan_failure_is_not_an_untracked_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = pursuit(tmp_path)

    def fail(path: Path) -> object:
        raise PermissionError("private-path-must-not-be-printed")

    monkeypatch.setattr(Path, "iterdir", fail)
    with pytest.raises(SalesforceSyncPartialError, match="local pursuit scan failed") as error:
        match_pursuit(path.parent, OPPORTUNITY_ID, workspace=tmp_path)
    assert "private-path" not in str(error.value)


@pytest.mark.parametrize("content", [b"\xff", b"a" * 4_000_001], ids=["invalid-utf8", "oversized-pursuit"])
def test_invalid_or_oversized_pursuit_is_not_skipped(tmp_path: Path, content: bytes) -> None:
    path = pursuit(tmp_path)
    path.write_bytes(content)
    with pytest.raises(FieldkitError, match="stable UTF-8"):
        read_local_pursuit(path, workspace=tmp_path)
