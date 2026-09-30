"""Integration tests for the contact enrichment pipeline (fieldkit.contact.enrich)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from fieldkit.enrich.schema import ContactRecord

DISCOVERY_TIMEOUT_SECONDS = 30

# ── TestContactEnrichPipeline (flattened) ───────────────────────────────────


@pytest.mark.integration
def test_contact_enrich_pipeline_discovery_runs(fictional_account: Path, tmp_path: Path) -> None:
    """Run the actual CLI against fictional data without inherited credentials.

    The child audit guard denies Python DNS and socket operations; it is a
    narrow test guard, not an operating-system process or network sandbox.
    """
    import yaml

    workspace = fictional_account.parents[1]
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    home = tmp_path / "home"
    config_path = home / ".config/fieldkit/config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        yaml.safe_dump({"fieldkit_home": str(workspace), "fieldkit_data": str(runtime)}), encoding="utf-8"
    )
    configuration = workspace / "config"
    configuration.mkdir()
    (configuration / "accounts.yaml").write_text(
        "accounts:\n  acme-corp:\n    domains: [example.com]\n", encoding="utf-8"
    )
    bootstrap = (
        "import runpy, sys\n"
        "def deny_network(event, args):\n"
        "    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname', "
        "'socket.gethostbyaddr', 'socket.sendto'}:\n"
        "        raise RuntimeError('Network access denied in contact discovery test')\n"
        "sys.addaudithook(deny_network)\n"
        "sys.argv = ['fieldkit', 'contact', 'enrich', '--discover', '--json']\n"
        "runpy.run_module('fieldkit', run_name='__main__')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", bootstrap],
        capture_output=True,
        text=True,
        timeout=DISCOVERY_TIMEOUT_SECONDS,
        cwd=tmp_path,
        env={
            "HOME": str(home),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "PYTHON_DOTENV_DISABLED": "1",
        },
        check=False,
    )

    assert result.returncode == 0, f"Discovery failed:\n{result.stderr[-2000:]}"
    assert len(result.stdout) < 10000
    summary = json.loads(result.stdout)
    assert summary["total"] == 2
    assert summary["by_account"] == {"acme-corp": 2}
    contacts = json.loads((workspace / "contact-enrich/contacts-raw.json").read_text(encoding="utf-8"))
    assert {contact["full_name"] for contact in contacts} == {"Jane Example", "Alex Example"}
    assert {contact["email"] for contact in contacts} == {"jane@example.com", "alex@example.com"}


@pytest.mark.integration
def test_contact_enrich_pipeline_skill_file_exists() -> None:
    """Root ``contact`` skill and its folded enrich op should exist.

    Parse the frontmatter mapping so YAML quoting and namespaces are verified.

    ``contact-enrich`` was folded into the ``contact`` root skill as
    ``ops/contact-enrich.md`` (D1 skill taxonomy, Wave 3, PR3) — it no longer carries
    its own frontmatter.
    """
    import yaml

    skill = Path("src/fieldkit/skills/contact/SKILL.md")
    assert skill.exists(), "src/fieldkit/skills/contact/SKILL.md not found"

    content = skill.read_text(encoding="utf-8")
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
    contact = ContactRecord.model_validate(
        {
            "full_name": "Test Contact",
            "company": "Test Corp",
            "account": "test",
            "email": "test@test.example.com",
            "source": "account-file",
            "confidence": "MEDIUM",
        }
    )
    assert contact.full_name == "Test Contact"
    assert contact.confidence == "MEDIUM"
