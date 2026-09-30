"""Both ingest pursuit matchers refuse unsafe or uninspectable inputs."""

from pathlib import Path
from typing import Literal
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.config import ConfigError
from fieldkit.errors import RoutingReadRetryableError
from fieldkit.ingest import router

pytestmark = pytest.mark.unit
Matcher = Literal["domains", "account"]


def _match(matcher: Matcher, root: Path) -> list[str]:
    if matcher == "domains":
        return router.route_with_pursuits(["acme-corp.example.com"], keywords=["phoenix"], data_root=root).pursuits
    return router.match_pursuits_for_account("acme", ["phoenix"], data_root=root)


@pytest.fixture
def routing_config(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    info: dict[str, object] = {"domains": ["acme-corp.example.com"], "pursuit_dir": "pursuits"}
    monkeypatch.setattr(router, "_load_accounts_config", lambda _root: {"accounts": {"acme": info}})
    return info


@pytest.mark.parametrize("matcher", ["domains", "account"])
@pytest.mark.parametrize(
    "path_value", ["../outside", "/outside", "pursuits/../outside", "pursuits\\outside", 42, [], None, False]
)
def test_invalid_configured_pursuit_path_rejected_before_reads(
    tmp_path: Path, routing_config: dict[str, object], matcher: Matcher, path_value: object
) -> None:
    routing_config["pursuit_dir"] = path_value
    with patch.object(router, "_read_pursuit_h1") as read, pytest.raises(ConfigError, match="pursuit") as caught:
        _match(matcher, tmp_path)
    assert str(tmp_path) not in str(caught.value)
    read.assert_not_called()


@pytest.mark.parametrize("matcher", ["domains", "account"])
@pytest.mark.parametrize("redirect", ["directory", "leaf"])
def test_pursuit_redirects_rejected_without_outside_reads(
    tmp_path: Path, routing_config: dict[str, object], matcher: Matcher, redirect: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "phoenix.md").write_text("# Phoenix\n", encoding="utf-8")
    root = tmp_path / "workspace"
    root.mkdir()
    if redirect == "directory":
        (root / "pursuits").symlink_to(outside, target_is_directory=True)
    else:
        (root / "pursuits").mkdir()
        (root / "pursuits" / "phoenix.md").symlink_to(outside / "phoenix.md")
    with patch.object(router, "_read_pursuit_h1") as read, pytest.raises(ConfigError, match="pursuit"):
        _match(matcher, root)
    read.assert_not_called()


@pytest.mark.parametrize("matcher", ["domains", "account"])
@pytest.mark.parametrize("payload", [b"x" * 4_000_001, b"\xff"], ids=["oversize", "invalid-utf8"])
def test_pursuit_text_must_be_bounded_utf8(
    tmp_path: Path, routing_config: dict[str, object], matcher: Matcher, payload: bytes
) -> None:
    pursuits = tmp_path / "pursuits"
    pursuits.mkdir()
    (pursuits / "phoenix.md").write_bytes(payload)
    with pytest.raises(ConfigError, match="pursuit") as caught:
        _match(matcher, tmp_path)
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize("matcher", ["domains", "account"])
@pytest.mark.parametrize("phase", ["open", "iterate"])
def test_directory_scan_failure_propagates_retryable_sanitized_error(
    tmp_path: Path, routing_config: dict[str, object], matcher: Matcher, phase: str
) -> None:
    pursuits = tmp_path / "pursuits"
    pursuits.mkdir()
    entries = MagicMock()
    entries.__iter__.side_effect = PermissionError(str(tmp_path))
    with (
        (
            patch("os.scandir", side_effect=PermissionError(str(tmp_path)))
            if phase == "open"
            else patch("os.scandir", return_value=entries)
        ),
        pytest.raises(RoutingReadRetryableError, match="pursuit") as caught,
    ):
        _match(matcher, tmp_path)
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize("matcher", ["domains", "account"])
def test_directory_entry_limit_is_nonpassing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, routing_config: dict[str, object], matcher: Matcher
) -> None:
    pursuits = tmp_path / "pursuits"
    pursuits.mkdir()
    for name in ("one.txt", "two.txt", "three.txt"):
        (pursuits / name).write_text("Fictional note", encoding="utf-8")
    monkeypatch.setattr(router, "_MAX_PURSUIT_DIRECTORY_ENTRIES", 2, raising=False)
    with pytest.raises(ConfigError, match="pursuit"):
        _match(matcher, tmp_path)


@pytest.mark.parametrize("matcher", ["domains", "account"])
@pytest.mark.parametrize("state", ["absent", "empty", "no_match", "match"])
def test_normal_pursuit_matching_and_configured_root_alias(
    tmp_path: Path, routing_config: dict[str, object], matcher: Matcher, state: str
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    if state != "absent":
        pursuits = root / "pursuits"
        pursuits.mkdir()
        if state != "empty":
            (pursuits / "planning.md").write_text(
                "# Phoenix\n" if state == "match" else "No heading\n", encoding="utf-8"
            )
    result = _match(matcher, alias)
    assert result == (["planning"] if state == "match" else [])


@pytest.mark.parametrize("matcher", ["domains", "account"])
def test_pursuit_snapshot_io_failure_is_retryable_and_sanitized(
    tmp_path: Path, routing_config: dict[str, object], matcher: Matcher
) -> None:
    pursuits = tmp_path / "pursuits"
    pursuits.mkdir()
    (pursuits / "phoenix.md").write_text("# Phoenix\n", encoding="utf-8")
    with (
        patch.object(router, "read_pursuit_text_snapshot", side_effect=OSError(str(tmp_path))),
        pytest.raises(RoutingReadRetryableError, match="pursuit") as caught,
    ):
        _match(matcher, tmp_path)
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize("matcher", ["domains", "account"])
def test_matching_is_sorted_and_heading_view_uses_characters(
    tmp_path: Path, routing_config: dict[str, object], matcher: Matcher
) -> None:
    pursuits = tmp_path / "pursuits"
    pursuits.mkdir()
    (pursuits / "z-project.md").write_text("# Phoenix\n", encoding="utf-8")
    (pursuits / "a-project.md").write_text("λ" * 450 + "\n# Phoenix\n", encoding="utf-8")
    result = _match(matcher, tmp_path)
    assert result == ["a-project", "z-project"]
