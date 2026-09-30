"""Semantic and structural contract for the public contract skill workflows."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parents[1] / "src/fieldkit/skills/contract"


def _read(relative: str) -> str:
    return (_ROOT / relative).read_text(encoding="utf-8")


def test_contract_root_keeps_missing_evidence_pending_and_advisory() -> None:
    content = _read("SKILL.md")
    one_line = " ".join(content.split())

    assert "not packaged PDF parsers, legal-compliance gates" in one_line
    assert "does not establish enforceability or authorize execution or sending" in one_line
    assert "document revision and page, section, or other inspectable location" in one_line
    assert "remain pending; do not count them as passing checks" in one_line
    assert "confined atomic write" in one_line
    assert "reread it" in one_line


def test_contract_extract_tracks_primary_source_coverage_and_write_approval() -> None:
    content = _read("ops/contract-extract.md")
    one_line = " ".join(content.split())

    assert "not a bundled PDF parser or legal review" in one_line
    assert "Missing tooling, inaccessible pages, or unreliable OCR stays unresolved" in one_line
    assert "verify its material claims against the selected primary sources" in one_line
    assert "Track which pages were actually inspected" in one_line
    assert "Map only relationships and precedence identified in the supplied documents" in one_line
    assert "confined atomic write, detect intervening edits" in one_line
    assert content.count("```") == 6


def test_contract_check_is_not_a_legal_or_send_gate() -> None:
    content = _read("ops/contract-check.md")
    one_line = " ".join(content.split())

    assert "advisory agent comparison, not legal advice, approval" in one_line
    assert "does not supersede the primary document" in one_line
    assert "mark the affected check pending" in one_line
    assert "source revision, locators, scope, coverage, and pending checks" in one_line
    assert "Do not send, sign, submit, or claim legal approval" in one_line
    assert content.count("```") == 2


def test_contract_drafting_requires_supplied_rates_rules_and_approval() -> None:
    redemption = " ".join(_read("ops/contract-redemption-form.md").split())
    deal_desk = " ".join(_read("ops/deal-desk.md").split())

    assert "positive currency-per-unit rate" in redemption
    assert "leave the calculation unresolved" in redemption
    assert "Do not overwrite an existing draft without approval" in redemption
    assert "Do not present the draft as executed, legally approved, or ready to send" in redemption
    assert "fieldkit does not supply a rate card" in deal_desk
    assert "without it, report approval status as undetermined" in deal_desk
    assert "This is a draft, not an approved quote" in deal_desk
