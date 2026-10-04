"""Regression checks for structured configuration examples in public docs."""

import json
import re
from pathlib import Path

import pytest
import yaml

import fieldkit.config._accounts as accounts_config
import fieldkit.config._loader as config_loader
from fieldkit.config._accounts import _load_accounts_yaml
from fieldkit.config._loader import clear_config_caches
from fieldkit.config._quota import get_pipeline_quota
from fieldkit.config._schema import _FieldkitConfig
from fieldkit.contact.enrich import apply_web
from fieldkit.enrich import _io as enrich_io

pytestmark = pytest.mark.unit

_CONFIG_REFERENCE = Path("docs/reference/config-file.md")
_CONTACT_GUIDE = Path("src/fieldkit/skills/contact/ops/contact-enrich.md")


def _yaml_examples() -> list[dict[str, object]]:
    """Load YAML fenced blocks exactly as published in the configuration reference."""
    content = _CONFIG_REFERENCE.read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\n(.*?)```", content, flags=re.DOTALL)
    return [yaml.safe_load(block) for block in blocks]


def test_configuration_reference_yaml_examples_are_accepted_by_their_consumers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Published YAML examples parse through the same configuration readers users invoke."""
    quota, shadowbot, accounts = _yaml_examples()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(quota), encoding="utf-8")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config_path)
    clear_config_caches()

    try:
        assert get_pipeline_quota() == {"target": 5_000_000, "period": "2026-H2"}
    finally:
        clear_config_caches()

    _FieldkitConfig.model_validate({"shadowbot": shadowbot})

    accounts_path = tmp_path / "accounts.yaml"
    accounts_path.write_text(yaml.safe_dump(accounts), encoding="utf-8")
    monkeypatch.setattr(accounts_config, "get_config_path", lambda _: accounts_path)
    clear_config_caches()

    try:
        assert _load_accounts_yaml() == accounts
    finally:
        clear_config_caches()


def test_gmail_guide_token_default_matches_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Published token guidance follows the configured runtime root."""
    import fieldkit.config._integrations as integrations_config

    monkeypatch.setattr(config_loader, "_load_raw_config", lambda: {})
    monkeypatch.setattr(integrations_config, "get_fieldkit_data", lambda: tmp_path)
    clear_config_caches()
    try:
        token_path = integrations_config.get_google_token_path()
        assert token_path.parent == tmp_path
        guide = Path("docs/guides/gmail.md").read_text(encoding="utf-8")
        documented = re.search(r"`<fieldkit_data>/([^`]+)`", guide)
        assert documented is not None
        assert token_path == tmp_path / documented.group(1)
    finally:
        clear_config_caches()


def test_contact_guide_storage_matches_configured_locations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Published storage claims use the real workspace helpers and filenames."""
    monkeypatch.setattr(enrich_io, "get_fieldkit_home", lambda: tmp_path)
    enrich_io._enrich_dir.cache_clear()
    enrich_io.contacts_memory_dir.cache_clear()
    try:
        data_directory = enrich_io.enrich_dir()
        assert data_directory == tmp_path / "contact-enrich"
        memory_directory = enrich_io.contacts_memory_dir()
        assert memory_directory == tmp_path / "memory" / "personal" / "contacts"
        guide = _CONTACT_GUIDE.read_text(encoding="utf-8")
        rows = dict(re.findall(r"\| ([^|]+?) \| `<fieldkit_home>/([^`]+)` \|", guide))
        assert tmp_path / rows["Discovered contacts"] == data_directory / enrich_io.CONTACTS_RAW
        assert tmp_path / rows["Enriched contacts"] == data_directory / enrich_io.CONTACTS_ENRICHED
        assert tmp_path / rows["Reviewed web results"] == data_directory / enrich_io.WEB_SEARCH_RESULTS
        assert (tmp_path / rows["Contact memory"]).parent == memory_directory
    finally:
        enrich_io._enrich_dir.cache_clear()
        enrich_io.contacts_memory_dir.cache_clear()


def test_contact_guide_web_example_is_consumed_by_apply_web(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The published JSON example fills a missing method without overwriting one."""
    import fieldkit.contact.enrich as contact_enrich

    guide = _CONTACT_GUIDE.read_text(encoding="utf-8")
    example = re.search(r"```json\n(.*?)```", guide, flags=re.DOTALL)
    assert example is not None
    records = json.loads(example.group(1))
    record = records[0]
    raw = [{"full_name": record["full_name"], "account": record["account"]}]
    (tmp_path / enrich_io.WEB_SEARCH_RESULTS).write_text(json.dumps(records), encoding="utf-8")
    monkeypatch.setattr(contact_enrich, "enrich_dir", lambda: tmp_path)
    monkeypatch.setattr(contact_enrich, "load_raw_contacts", lambda: raw)
    result = apply_web(account=record["account"])
    assert result.updated_fields == 1
    written = json.loads((tmp_path / enrich_io.CONTACTS_RAW).read_text(encoding="utf-8"))
    assert written[0]["email"] == record["email"]

    raw[0]["email"] = "existing@example.com"
    result = apply_web(account=record["account"])
    assert result.updated_fields == 0
    written = json.loads((tmp_path / enrich_io.CONTACTS_RAW).read_text(encoding="utf-8"))
    assert written[0]["email"] == "existing@example.com"
