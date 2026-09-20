"""Tests for the source and rendered public-documentation boundaries."""

import json
from pathlib import Path

import pytest

from scripts import check_public_docs

pytestmark = pytest.mark.unit

_PRIVATE_TRACKER_ID = "BUG" + "-123"


def _write(site: Path, relative: str, content: str = "public") -> None:
    target = site / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _write_policy(repo: Path, *, public_site: list[str], excluded: list[str]) -> None:
    policy = {
        "schema_version": 1,
        "categories": {
            "public_entrypoint": ["README.md"],
            "public_site": public_site,
            "public_repository_only": [],
            "private_history_excluded": excluded,
        },
        "issue_forms": {
            "required": ["bug.yml"],
            "allowed_labels": ["bug"],
            "contact_links": ["https://example.com/security", "https://example.com/support"],
        },
    }
    _write(repo, "docs/release-readiness/public-surface-policy.json", json.dumps(policy))


def _write_complete_public_pages(site: Path) -> None:
    for relative in check_public_docs._PUBLIC_PAGES:
        _write(site, relative)


def test_validate_accepts_classified_public_pages_and_assets(tmp_path: Path) -> None:
    _write_complete_public_pages(tmp_path)
    _write(tmp_path, "css/base.css")
    _write(tmp_path, "search/search_index.json", '{"docs": []}')

    findings = check_public_docs.validate(tmp_path)

    assert findings == ()


def test_validate_allows_public_organization_and_platform_context(tmp_path: Path) -> None:
    _write(tmp_path, "compatibility/index.html", "Example Enterprise Linux is an optional platform.")

    findings = check_public_docs.validate(tmp_path, require_complete=False)

    assert findings == ()


def test_validate_rejects_unclassified_rendered_paths(tmp_path: Path) -> None:
    _write(tmp_path, "index.html")
    _write(tmp_path, "release-readiness/internal.json", "{}")

    findings = check_public_docs.validate(tmp_path, require_complete=False)

    assert findings == (check_public_docs.Finding("DOC001", "release-readiness/internal.json"),)


def test_validate_rejects_unclassified_files_under_asset_directories(tmp_path: Path) -> None:
    _write(tmp_path, "img/customer-notes.txt")

    findings = check_public_docs.validate(tmp_path, require_complete=False)

    assert findings == (check_public_docs.Finding("DOC001", "img/customer-notes.txt"),)


def test_validate_rejects_symlinked_directories(tmp_path: Path) -> None:
    outside = tmp_path.parent / "private-docs"
    outside.mkdir()
    (tmp_path / "guides").symlink_to(outside, target_is_directory=True)

    findings = check_public_docs.validate(tmp_path, require_complete=False)

    assert findings == (check_public_docs.Finding("DOC003", "guides"),)


@pytest.mark.parametrize(
    ("content", "criterion_id"),
    [
        ("internal employer name", "DOC101"),
        (str(Path("/") / "home" / "example-user" / "private"), "DOC102"),
        (f"private tracker {_PRIVATE_TRACKER_ID}", "DOC103"),
        ("see ops-runbook", "DOC104"),
    ],
)
def test_validate_rejects_private_markers(tmp_path: Path, content: str, criterion_id: str) -> None:
    if criterion_id == "DOC101":
        content = "Red" + " Hat internal"
    _write(tmp_path, "index.html", content)

    findings = check_public_docs.validate(tmp_path, require_complete=False)

    assert findings == (check_public_docs.Finding(criterion_id, "index.html"),)


def test_validate_rejects_missing_site(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(ValueError, match="site directory does not exist"):
        check_public_docs.validate(missing)


def test_validate_rejects_empty_rendered_site(tmp_path: Path) -> None:
    findings = check_public_docs.validate(tmp_path)

    assert check_public_docs.Finding("DOC002", "index.html") in findings


def test_repository_source_surfaces_are_classified() -> None:
    findings = check_public_docs.validate_sources()

    assert findings == ()


def test_validate_sources_rejects_unclassified_document(tmp_path: Path) -> None:
    _write_policy(tmp_path, public_site=["docs/index.md"], excluded=["docs/release-readiness/**"])
    _write(tmp_path, "README.md")
    _write(tmp_path, "docs/index.md")
    _write(tmp_path, "docs/new-internal-note.md")

    findings = check_public_docs.validate_sources(tmp_path)

    assert findings == (check_public_docs.Finding("DOC201", "docs/new-internal-note.md"),)


def test_validate_sources_rejects_ambiguous_classification(tmp_path: Path) -> None:
    _write_policy(
        tmp_path,
        public_site=["docs/release-readiness/private.json"],
        excluded=["docs/release-readiness/**"],
    )
    _write(tmp_path, "README.md")
    _write(tmp_path, "docs/release-readiness/private.json")

    findings = check_public_docs.validate_sources(tmp_path)

    assert findings == (check_public_docs.Finding("DOC202", "docs/release-readiness/private.json"),)


def test_repository_issue_forms_and_routes_are_valid() -> None:
    findings = check_public_docs.validate_community()

    assert findings == ()


def test_validate_community_rejects_unknown_form_label(tmp_path: Path) -> None:
    _write_policy(tmp_path, public_site=["docs/index.md"], excluded=["docs/release-readiness/**"])
    _write(
        tmp_path,
        ".github/ISSUE_TEMPLATE/bug.yml",
        "name: Bug\ndescription: Report a bug\nlabels: [private]\nbody:\n  - type: markdown\n"
        "    attributes:\n      value: Explain the bug\n",
    )
    _write(
        tmp_path,
        ".github/ISSUE_TEMPLATE/config.yml",
        "blank_issues_enabled: false\ncontact_links:\n"
        "  - name: Security\n    url: https://example.com/security\n    about: Private reports\n"
        "  - name: Support\n    url: https://example.com/support\n    about: Usage help\n",
    )

    findings = check_public_docs.validate_community(tmp_path)

    assert findings == (check_public_docs.Finding("DOC302", ".github/ISSUE_TEMPLATE/bug.yml"),)


@pytest.mark.parametrize(
    "body",
    [
        "  - type: dropdown\n    id: choice\n    attributes:\n      label: Choose\n      options: [42]\n",
        "  - type: checkboxes\n    id: consent\n    attributes:\n      label: Confirm\n      options: [42]\n",
        "  - type: checkboxes\n    id: consent\n    attributes:\n      label: Confirm\n"
        "      options:\n        - label: I agree\n          required: nope\n",
    ],
)
def test_validate_community_rejects_malformed_form_options(tmp_path: Path, body: str) -> None:
    _write_policy(tmp_path, public_site=["docs/index.md"], excluded=["docs/release-readiness/**"])
    _write(
        tmp_path,
        ".github/ISSUE_TEMPLATE/bug.yml",
        "name: Bug\ndescription: Report a bug\nlabels: [bug]\nbody:\n" + body,
    )
    _write(
        tmp_path,
        ".github/ISSUE_TEMPLATE/config.yml",
        "blank_issues_enabled: false\ncontact_links:\n"
        "  - name: Security\n    url: https://example.com/security\n    about: Private reports\n"
        "  - name: Support\n    url: https://example.com/support\n    about: Usage help\n",
    )

    findings = check_public_docs.validate_community(tmp_path)

    assert findings == (check_public_docs.Finding("DOC302", ".github/ISSUE_TEMPLATE/bug.yml"),)


def test_validate_community_rejects_public_blanks_or_route_drift(tmp_path: Path) -> None:
    _write_policy(tmp_path, public_site=["docs/index.md"], excluded=["docs/release-readiness/**"])
    _write(
        tmp_path,
        ".github/ISSUE_TEMPLATE/bug.yml",
        "name: Bug\ndescription: Report a bug\nlabels: [bug]\nbody:\n  - type: markdown\n"
        "    attributes:\n      value: Explain the bug\n",
    )
    _write(tmp_path, ".github/ISSUE_TEMPLATE/config.yml", "blank_issues_enabled: true\ncontact_links: []\n")

    findings = check_public_docs.validate_community(tmp_path)

    assert findings == (check_public_docs.Finding("DOC303", ".github/ISSUE_TEMPLATE/config.yml"),)


def test_main_returns_error_for_missing_site(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result = check_public_docs.main([str(tmp_path / "missing"), "--repo-root", str(tmp_path)])

    output = capsys.readouterr().out
    assert result == 2
    assert "Public documentation check: ERROR" in output
