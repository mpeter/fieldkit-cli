"""Semantic ownership of published-release and security support promises."""

import shutil
from pathlib import Path

import pytest

from scripts import _release_policy, markdown_tables

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SUPPORTED_RELEASES = (
    "Only the latest patch release of the latest minor version receives bug fixes and security fixes. "
    "Older patch releases and older minor versions are unsupported; upgrade to the supported release "
    "before asking for a fix. The `main` branch receives fixes on a best-effort basis during development "
    "and is not a supported published release."
)
_SECURITY_SUPPORT_ROWS = {
    "latest-patch-of-latest-minor": (
        ("Latest patch of the latest minor release", "Supported"),
        ("Older patch releases and older minor releases", "Unsupported"),
        ("Unreleased `main`", "Best-effort development; not a supported published release"),
    )
}


def _section(text: str, heading: str) -> str:
    marker = f"## {heading}\n"
    return text.split(marker, 1)[1].split("\n## ", 1)[0].strip() if marker in text else ""


def _support_policy_findings(repo: Path) -> tuple[str, ...]:
    policy = _release_policy.load_policy(repo / _release_policy.POLICY_PATH)
    support = (repo / policy.support_policy_source).read_text(encoding="utf-8")
    security = _section((repo / "SECURITY.md").read_text(encoding="utf-8"), "Supported versions")
    findings: list[str] = []
    if " ".join(_section(support, "Supported releases").split()) != _SUPPORTED_RELEASES:
        findings.append("canonical supported-release policy differs")
    tables = markdown_tables.parse_markdown_tables(security)
    if (
        len(tables) != 1
        or tables[0].header != ("Version", "Security support")
        or tables[0].rows != _SECURITY_SUPPORT_ROWS[policy.supported_line]
    ):
        findings.append("security support table differs from the release policy")
    prose = security
    if len(tables) == 1:
        prose = prose.replace(tables[0].source_text, "")
    expected_link = f"[{policy.support_policy_source}]({policy.support_policy_source}#supported-releases)"
    if " ".join(prose.split()) != f"Security fixes follow the supported-release policy in {expected_link}.":
        findings.append("security support must defer to the canonical supported-release policy")
    return tuple(findings)


@pytest.fixture
def support_policy_repo(tmp_path: Path) -> Path:
    policy_path = tmp_path / _release_policy.POLICY_PATH
    policy_path.parent.mkdir(parents=True)
    shutil.copy2(_REPO_ROOT / _release_policy.POLICY_PATH, policy_path)
    for name in ("SUPPORT.md", "SECURITY.md"):
        shutil.copy2(_REPO_ROOT / name, tmp_path / name)
    return tmp_path


def test_documentation_support_matches_the_authoritative_release_policy() -> None:
    """A reader receives the same support boundary from canonical policy and security guidance."""
    findings = _support_policy_findings(_REPO_ROOT)

    assert findings == ()


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ("Latest patch of the latest minor release", "Published 1.x releases"),
        ("Older patch releases and older minor releases | Unsupported", "Older minor releases | Supported"),
        (
            "Latest patch of the latest minor release | Supported",
            "Latest patch of the latest minor release | Not supported",
        ),
        ("not a supported published release", "a supported published release"),
    ],
)
def test_security_support_rejects_changed_release_promises(
    support_policy_repo: Path, original: str, replacement: str
) -> None:
    """Broad promises, older-minor support, and negated statuses cannot pass on matching keywords."""
    path = support_policy_repo / "SECURITY.md"
    content = path.read_text(encoding="utf-8")
    assert original in content
    path.write_text(content.replace(original, replacement), encoding="utf-8")

    findings = _support_policy_findings(support_policy_repo)

    assert findings == ("security support table differs from the release policy",)


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ("Only the latest patch release of the latest minor version", "All published 1.x releases"),
        ("receives bug fixes and security fixes", "does not receive bug fixes or security fixes"),
        ("older minor versions are unsupported", "older minor versions are supported"),
    ],
)
def test_canonical_support_rejects_changed_release_promises(
    support_policy_repo: Path, original: str, replacement: str
) -> None:
    """The machine policy's named support source cannot contradict the approved support boundary."""
    policy = _release_policy.load_policy(support_policy_repo / _release_policy.POLICY_PATH)
    path = support_policy_repo / policy.support_policy_source
    content = path.read_text(encoding="utf-8")
    assert original in content
    path.write_text(content.replace(original, replacement), encoding="utf-8")

    findings = _support_policy_findings(support_policy_repo)

    assert findings == ("canonical supported-release policy differs",)


@pytest.mark.parametrize("replacement", ["README.md", "SUPPORT.md#before-asking"])
def test_security_support_requires_the_canonical_policy_link(support_policy_repo: Path, replacement: str) -> None:
    """Security support must link to the exact source and section designated by the release policy."""
    path = support_policy_repo / "SECURITY.md"
    content = path.read_text(encoding="utf-8")
    assert "SUPPORT.md#supported-releases" in content
    path.write_text(content.replace("SUPPORT.md#supported-releases", replacement), encoding="utf-8")

    findings = _support_policy_findings(support_policy_repo)

    assert findings == ("security support must defer to the canonical supported-release policy",)
