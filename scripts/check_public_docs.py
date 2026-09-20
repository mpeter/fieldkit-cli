#!/usr/bin/env python3
"""Validate the rendered MkDocs tree against fieldkit's public-site boundary."""

import argparse
import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SURFACE_POLICY = Path("docs/release-readiness/public-surface-policy.json")
_SURFACE_CATEGORIES = frozenset(
    {"public_entrypoint", "public_site", "public_repository_only", "private_history_excluded"}
)
_ROOT_SURFACES = frozenset(
    {
        ".github/CODEOWNERS",
        ".github/PULL_REQUEST_TEMPLATE.md",
        "AGENTS.md",
        "CLAUDE.md",
        "CONTRIBUTING.md",
        "GOVERNANCE.md",
        "LICENSE",
        "ROADMAP.md",
        "README.md",
        "SECURITY.md",
        "SUPPORT.md",
    }
)
_SURFACE_TREES = (".claude", ".github/ISSUE_TEMPLATE", ".github/agents", ".opencode", "docs")

_PUBLIC_PAGES = frozenset(
    {
        "404.html",
        "cli-reference/index.html",
        "compatibility/index.html",
        "concepts/index.html",
        "dependency-security/index.html",
        "getting-started/index.html",
        "guides/gmail/index.html",
        "guides/init/index.html",
        "guides/morning-brief/index.html",
        "guides/pipeline-workflow/index.html",
        "guides/salesforce-auth/index.html",
        "guides/shadowbot-auth/index.html",
        "guides/watchers/index.html",
        "index.html",
        "integrations/index.html",
        "privacy/index.html",
        "reference/config-file/index.html",
        "reference/environment-vars/index.html",
        "reference/exit-codes/index.html",
        "reference/troubleshooting/index.html",
        "releases/index.html",
        "user-guide/index.html",
    }
)
_GENERATED_ASSETS = frozenset(
    {
        "css/base.css",
        "css/bootstrap.min.css",
        "css/bootstrap.min.css.map",
        "css/brands.min.css",
        "css/fontawesome.min.css",
        "css/solid.min.css",
        "css/v4-font-face.min.css",
        "img/favicon.ico",
        "img/grid.png",
        "js/base.js",
        "js/bootstrap.bundle.min.js",
        "js/bootstrap.bundle.min.js.map",
        "js/darkmode.js",
        "search/lunr.js",
        "search/main.js",
        "search/search_index.json",
        "search/worker.js",
        "sitemap.xml",
        "sitemap.xml.gz",
        "webfonts/fa-brands-400.ttf",
        "webfonts/fa-brands-400.woff2",
        "webfonts/fa-regular-400.ttf",
        "webfonts/fa-regular-400.woff2",
        "webfonts/fa-solid-900.ttf",
        "webfonts/fa-solid-900.woff2",
        "webfonts/fa-v4compatibility.ttf",
        "webfonts/fa-v4compatibility.woff2",
    }
)
_SCANNED_TEXT = frozenset({".html", ".json"})
_FORBIDDEN_TEXT = (
    (
        "DOC101",
        re.compile(r"(?i)(?:red[ -]?hat\s+(?:internal|confidential|only)|@redhat\.com)"),  # pii-guard: ignore
    ),
    ("DOC102", re.compile(r"/(?:home|Users)/[A-Za-z][A-Za-z0-9_.-]{1,30}/")),
    ("DOC103", re.compile(r"(?i)(?<![A-Za-z0-9])(?:bug|enh|bi)-[0-9]+(?![A-Za-z0-9])")),
    ("DOC104", re.compile(r"(?i)(?:ops-runbook|succession-plan|openchamber)")),
)


@dataclass(frozen=True, order=True)
class Finding:
    """One payload-safe rendered-site boundary violation."""

    criterion_id: str
    path: str


@dataclass(frozen=True)
class SurfacePolicy:
    """Versioned source and issue-routing classifications."""

    categories: dict[str, tuple[str, ...]]
    required_issue_forms: tuple[str, ...]
    allowed_issue_labels: frozenset[str]
    contact_links: tuple[str, ...]


def _string_list(value: object, subject: str) -> tuple[str, ...]:
    """Return a validated, non-empty list of policy strings."""
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{_SURFACE_POLICY}: {subject} must be a non-empty string list")
    return tuple(item for item in value if isinstance(item, str))


def _load_surface_policy(repo_root: Path) -> SurfacePolicy:
    """Load the versioned source-surface classification policy."""
    raw = json.loads((repo_root / _SURFACE_POLICY).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError(f"{_SURFACE_POLICY}: schema_version must be 1")
    categories = raw.get("categories")
    if not isinstance(categories, dict) or set(categories) != _SURFACE_CATEGORIES:
        raise ValueError(f"{_SURFACE_POLICY}: categories must be the four supported classifications")
    result: dict[str, tuple[str, ...]] = {}
    for category, value in categories.items():
        if not isinstance(value, list) or not all(isinstance(pattern, str) and pattern for pattern in value):
            raise ValueError(f"{_SURFACE_POLICY}: category {category} must be a string list")
        patterns = tuple(pattern for pattern in value if isinstance(pattern, str))
        if not isinstance(category, str) or not all(not pattern.startswith("/") for pattern in patterns):
            raise ValueError(f"{_SURFACE_POLICY}: patterns must be non-empty repository-relative strings")
        result[category] = patterns
    issue_forms = raw.get("issue_forms")
    if not isinstance(issue_forms, dict):
        raise ValueError(f"{_SURFACE_POLICY}: issue_forms must be an object")
    return SurfacePolicy(
        categories=result,
        required_issue_forms=_string_list(issue_forms.get("required"), "issue_forms.required"),
        allowed_issue_labels=frozenset(_string_list(issue_forms.get("allowed_labels"), "issue_forms.allowed_labels")),
        contact_links=_string_list(issue_forms.get("contact_links"), "issue_forms.contact_links"),
    )


def _source_surface_paths(repo_root: Path) -> tuple[str, ...]:
    """Return documentation, community, and agent paths that require classification."""
    candidates: set[str] = set()
    for relative in _ROOT_SURFACES:
        candidate = repo_root / relative
        if candidate.is_file() or candidate.is_symlink():
            candidates.add(relative)
    for relative in _SURFACE_TREES:
        tree = repo_root / relative
        if not tree.exists():
            continue
        for candidate in tree.rglob("*"):
            if candidate.is_file() or candidate.is_symlink():
                candidates.add(candidate.relative_to(repo_root).as_posix())
    return tuple(sorted(candidates))


def validate_sources(repo_root: Path = _REPO_ROOT) -> tuple[Finding, ...]:
    """Reject missing or ambiguous classifications for repository information surfaces."""
    categories = _load_surface_policy(repo_root).categories
    findings: list[Finding] = []
    for path in _source_surface_paths(repo_root):
        matches = [
            category
            for category, patterns in categories.items()
            if any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)
        ]
        if not matches:
            findings.append(Finding("DOC201", path))
        elif len(matches) > 1:
            findings.append(Finding("DOC202", path))
    return tuple(sorted(findings))


def _valid_form(document: object, allowed_labels: frozenset[str]) -> bool:
    """Return whether an issue form satisfies the public routing contract."""
    if not isinstance(document, dict):
        return False
    if not all(isinstance(document.get(key), str) and document[key] for key in ("name", "description")):
        return False
    labels = document.get("labels", [])
    if not isinstance(labels, list) or not all(isinstance(label, str) and label in allowed_labels for label in labels):
        return False
    body = document.get("body")
    if not isinstance(body, list) or not body:
        return False
    seen_ids: set[str] = set()
    for element in body:
        if not isinstance(element, dict) or element.get("type") not in {
            "checkboxes",
            "dropdown",
            "input",
            "markdown",
            "textarea",
        }:
            return False
        attributes = element.get("attributes")
        if not isinstance(attributes, dict):
            return False
        if element["type"] == "markdown":
            if not isinstance(attributes.get("value"), str) or not attributes["value"]:
                return False
            continue
        element_id = element.get("id")
        if not isinstance(element_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", element_id):
            return False
        if element_id in seen_ids or not isinstance(attributes.get("label"), str):
            return False
        seen_ids.add(element_id)
        options = attributes.get("options")
        if element["type"] == "dropdown" and (
            not isinstance(options, list)
            or not options
            or not all(isinstance(option, str) and option for option in options)
        ):
            return False
        if element["type"] == "checkboxes" and (
            not isinstance(options, list)
            or not options
            or not all(
                isinstance(option, dict)
                and isinstance(option.get("label"), str)
                and bool(option["label"])
                and ("required" not in option or isinstance(option["required"], bool))
                for option in options
            )
        ):
            return False
        validations = element.get("validations", {})
        if not isinstance(validations, dict) or any(not isinstance(value, bool) for value in validations.values()):
            return False
    return True


def validate_community(repo_root: Path = _REPO_ROOT) -> tuple[Finding, ...]:
    """Validate the issue forms and their private/support routing contract."""
    policy = _load_surface_policy(repo_root)
    template_root = repo_root / ".github" / "ISSUE_TEMPLATE"
    findings: list[Finding] = []
    actual_forms = {path.name for path in template_root.glob("*.yml") if path.name != "config.yml"}
    if actual_forms != set(policy.required_issue_forms):
        findings.append(Finding("DOC301", ".github/ISSUE_TEMPLATE"))
    for name in sorted(actual_forms):
        path = template_root / name
        if not _valid_form(yaml.safe_load(path.read_text(encoding="utf-8")), policy.allowed_issue_labels):
            findings.append(Finding("DOC302", path.relative_to(repo_root).as_posix()))
    config_path = template_root / "config.yml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    links = config.get("contact_links") if isinstance(config, dict) else None
    urls = tuple(link.get("url") for link in links if isinstance(link, dict)) if isinstance(links, list) else ()
    if not isinstance(config, dict) or config.get("blank_issues_enabled") is not False or urls != policy.contact_links:
        findings.append(Finding("DOC303", config_path.relative_to(repo_root).as_posix()))
    return tuple(sorted(findings))


def _path_is_classified(path: str) -> bool:
    """Return whether *path* is an approved page or generated site asset."""
    return path in _PUBLIC_PAGES or path in _GENERATED_ASSETS


def validate(site_root: Path, *, require_complete: bool = True) -> tuple[Finding, ...]:
    """Return deterministic findings for an already-rendered MkDocs tree."""
    if not site_root.is_dir():
        raise ValueError(f"site directory does not exist: {site_root}")

    findings: list[Finding] = []
    if require_complete:
        actual_files = {
            candidate.relative_to(site_root).as_posix()
            for candidate in site_root.rglob("*")
            if candidate.is_file() and not candidate.is_symlink()
        }
        findings.extend(Finding("DOC002", path) for path in sorted(_PUBLIC_PAGES - actual_files))
    for candidate in sorted(site_root.rglob("*")):
        relative = candidate.relative_to(site_root).as_posix()
        if candidate.is_symlink():
            findings.append(Finding("DOC003", relative))
            continue
        if candidate.is_dir():
            continue
        if not _path_is_classified(relative):
            findings.append(Finding("DOC001", relative))
            continue
        if candidate.suffix not in _SCANNED_TEXT:
            continue
        text = candidate.read_text(encoding="utf-8")
        for criterion_id, pattern in _FORBIDDEN_TEXT:
            if pattern.search(text) is not None:
                findings.append(Finding(criterion_id, relative))
    return tuple(sorted(set(findings)))


def main(argv: list[str] | None = None) -> int:
    """Validate a site tree and return a process status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site_root", type=Path)
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    args = parser.parse_args(argv)
    try:
        repo_root = args.repo_root.resolve()
        findings = validate_sources(repo_root) + validate_community(repo_root) + validate(args.site_root.resolve())
    except (json.JSONDecodeError, OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        print(f"Public documentation check: ERROR: {exc}")
        return 2
    if findings:
        print(f"Public documentation check: FAIL ({len(findings)} finding(s))")
        for finding in findings:
            print(f"  {finding.criterion_id} {finding.path}")
        return 1
    print("Public documentation check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
