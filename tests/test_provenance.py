"""Unit tests for the derived-doc provenance marker (implementation change).

Spec ref: openspec/changes/derived-doc-exhaust-marker/specs/doc-provenance/spec.md
— Requirement: Marker rendering has one home and is injection-safe.
"""

import pytest
import yaml

from fieldkit.provenance import derived_doc_banner, derived_doc_marker

pytestmark = pytest.mark.unit


def _frontmatter(marker: str) -> dict:
    """Parse the frontmatter block of a rendered marker."""
    docs = list(yaml.safe_load_all(marker))
    assert docs, "marker must contain at least one YAML document"
    return docs[0]


@pytest.mark.parametrize("caste", ["derived", "summary"])
def test_marker_renders_delimited_frontmatter_with_caste(caste: str) -> None:
    """Marker is a single ----delimited block carrying caste, sources, and generator."""
    marker = derived_doc_marker(
        caste=caste,  # type: ignore[arg-type]  # parametrize passes str; runtime values are valid literals
        derived_from=["contacts-raw.json", "contacts-enriched.json"],
        generated_by="fieldkit enrich generate-report",
    )

    # CR-014: assert directly on the return value first.
    assert marker.startswith("---\n")
    assert marker.endswith("\n---\n")

    fm = _frontmatter(marker)
    assert fm == {
        "caste": caste,
        "derived_from": ["contacts-raw.json", "contacts-enriched.json"],
        "generated_by": "fieldkit enrich generate-report",
    }


@pytest.mark.parametrize(
    "hostile",
    [
        "source: with colon",
        "fieldkit --help (live CLI surface)",
        "before --- after",
        'quo"ted',
        "multi\n---\nline",
    ],
)
def test_hostile_source_refs_round_trip(hostile: str) -> None:
    """YAML-hostile refs cannot inject a document separator (historic regression class).

    Scenario: Source ref containing YAML-hostile characters cannot break the block.
    """
    marker = derived_doc_marker(caste="summary", derived_from=[hostile], generated_by="t: tool")

    assert marker.startswith("---\n")
    fm = _frontmatter(marker)
    assert fm["derived_from"] == [hostile]
    assert fm["generated_by"] == "t: tool"
    assert fm["caste"] == "summary"


def test_empty_derived_from_rejected() -> None:
    """A caste marker without sources defeats the point — hard error."""
    with pytest.raises(ValueError, match="derived_from must name at least one source"):
        derived_doc_marker(caste="derived", derived_from=[], generated_by="tool")


def test_banner_names_exhaust_not_system_of_record() -> None:
    """Scenario: Banner is present in rendered body — wording contract."""
    banner = derived_doc_banner()

    assert banner.startswith("> ")
    assert "not a system of record" in banner
    assert "derived_from" in banner
