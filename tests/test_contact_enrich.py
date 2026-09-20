"""Integration tests for the contact enrichment pipeline (fieldkit.contact.enrich)."""

import subprocess
from pathlib import Path

import pytest

from fieldkit.enrich.schema import ContactRecord
from tests.conftest import needs_data

# ── TestContactEnrichPipeline (flattened) ───────────────────────────────────


@pytest.fixture
def _contact_dir() -> Path:
    """Provide the contact domain directory path."""
    contact_dir = Path("src/fieldkit/contact")
    assert contact_dir.exists(), "src/fieldkit/contact/ not found"
    return contact_dir


@pytest.mark.integration
@needs_data
def test_contact_enrich_pipeline_discovery_runs(_contact_dir: Path) -> None:
    """`fieldkit contact enrich --discover` should run without errors."""
    import os

    import yaml

    config_path = Path("~/.config/fieldkit/config.yaml").expanduser()
    if not config_path.exists():
        pytest.skip("fieldkit config not found — skip subprocess integration test")
    try:
        cfg = yaml.safe_load(config_path.read_text())
        if not isinstance(cfg, dict) or "fieldkit_home" not in cfg:
            pytest.skip("fieldkit config missing fieldkit_home key — skip subprocess integration test")
    except Exception:
        pytest.skip("fieldkit config unreadable — skip subprocess integration test")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path().resolve())
    result = subprocess.run(
        ["python3", "-m", "fieldkit", "contact", "enrich", "--discover"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )

    assert result.returncode == 0, f"Discovery failed:\n{result.stderr}"


@pytest.mark.integration
def test_contact_enrich_pipeline_skill_file_exists() -> None:
    """Root ``contact`` skill and its folded enrich op should exist.

    Reads only tracked repo files (no workspace data), so it is not gated behind
    ``@needs_data`` — that gate previously hid this assertion from CI while it went
    stale against the ``slash: true`` → ``metadata.opencode/slash: "true"`` migration
    (#1365). Parse the frontmatter and check the mapping rather than substring-matching
    raw YAML, which is what let a quoting/namespacing change slip past before.

    ``contact-enrich`` was folded into the ``contact`` root skill as
    ``ops/contact-enrich.md`` (D1 skill taxonomy, Wave 3, PR3) — it no longer carries
    its own frontmatter.
    """
    import yaml

    skill = Path("src/fieldkit/skills/contact/SKILL.md")
    assert skill.exists(), "src/fieldkit/skills/contact/SKILL.md not found"

    content = skill.read_text()
    assert content.startswith("---"), "Missing frontmatter"
    frontmatter = yaml.safe_load(content.split("---", 2)[1])
    assert frontmatter["name"] == "contact"
    assert frontmatter["metadata"]["opencode/slash"] == "true"

    enrich_op = Path("src/fieldkit/skills/contact/ops/contact-enrich.md")
    assert enrich_op.exists(), "src/fieldkit/skills/contact/ops/contact-enrich.md not found"


# ── TestContactEnrichSchema (flattened) ─────────────────────────────────────


@pytest.mark.unit
def test_contact_enrich_schema_schema_import() -> None:
    """Schema should be importable from fieldkit.enrich."""
    contact = ContactRecord(
        full_name="Test Contact",
        company="Test Corp",
        account="test",
        email="test@test.example.com",
        source="account-file",
        confidence="MEDIUM",
    )
    assert contact.full_name == "Test Contact"
    assert contact.confidence == "MEDIUM"
