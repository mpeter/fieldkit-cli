"""Check the Gmail package README against its command registry and links."""

import re
from pathlib import Path

import pytest

from fieldkit.commands.gmail.cli import _COMMANDS

pytestmark = pytest.mark.unit

README = Path("src/fieldkit/commands/gmail/README.md")


def test_readme_command_map_matches_lazy_registry() -> None:
    text = README.read_text(encoding="utf-8")
    for name in ("sync", "query", "account-tags", "enrich-pursuits", "import-cache"):
        assert name in _COMMANDS
        assert f"fieldkit gmail {name}" in text
    assert _COMMANDS["sync"].profile == "google"
    assert all(_COMMANDS[name].profile is None for name in ("query", "account-tags", "enrich-pursuits", "import-cache"))
    assert "The other commands below do not require that profile." in text


def test_readme_links_resolve_to_current_sources() -> None:
    text = README.read_text(encoding="utf-8")
    links = re.findall(r"\[[^]]+\]\(([^)]+)\)", text)
    assert links
    for link in links:
        assert (README.parent / link).resolve().exists(), link
