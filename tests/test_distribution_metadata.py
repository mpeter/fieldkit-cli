"""Publication metadata contract for the fieldkit 1.0 distribution."""

import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_distribution_command_and_import_identities_are_distinct() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["name"] == "fieldkit-cli"
    assert metadata["project"]["version"] == "1.0.0"
    assert metadata["project"]["scripts"] == {"fieldkit": "fieldkit.__main__:main"}
    assert metadata["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/fieldkit"]


def test_distribution_metadata_is_publication_complete() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["readme"] == {"file": "README.md", "content-type": "text/markdown"}
    assert project["license"] == "Apache-2.0"
    assert project["license-files"] == ["LICENSE"]
    assert project["maintainers"]
    assert set(project["urls"]) == {"Documentation", "Issues", "Repository"}
    assert project["keywords"]
    assert "Development Status :: 4 - Beta" in project["classifiers"]
    for version in ("3.11", "3.12", "3.13", "3.14"):
        assert f"Programming Language :: Python :: {version}" in project["classifiers"]
