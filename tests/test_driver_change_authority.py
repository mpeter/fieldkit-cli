"""Tests for pure submitted-change authority validation."""

import pytest

from fieldkit.driver.change_authority import ChangeAuthorityError, validate_change_authority

pytestmark = pytest.mark.unit


def test_accepts_exact_file_and_descendant_directory_paths() -> None:
    payload = b"M\0README.md\0A\0src/fieldkit/new.py\0"

    result = validate_change_authority(payload, frozenset({"README.md", "src/"}))

    assert result is None


def test_checks_both_rename_endpoints() -> None:
    payload = b"R100\0src/old.py\0outside.py\0"

    with pytest.raises(ChangeAuthorityError, match="exceed declared covers"):
        validate_change_authority(payload, frozenset({"src/"}))


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"M\0../outside\0", id="traversal"),
        pytest.param(b"M\0/path\0", id="absolute"),
        pytest.param(b"M\0bad\\path\0", id="backslash"),
        pytest.param(b"M\0\xff\0", id="non-utf8"),
        pytest.param(b"M\0", id="missing-path"),
        pytest.param(b"Q\0README.md\0", id="unknown-status"),
    ],
)
def test_rejects_malformed_or_unsafe_diff_records(payload: bytes) -> None:
    with pytest.raises(ChangeAuthorityError, match=r"malformed|unsafe"):
        validate_change_authority(payload, frozenset({"README.md"}))


def test_rejects_unbounded_diff_before_parsing() -> None:
    with pytest.raises(ChangeAuthorityError, match="byte limit"):
        validate_change_authority(b"x" * (2 * 1024 * 1024 + 1), frozenset({"src/"}))
