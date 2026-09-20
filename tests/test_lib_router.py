"""Tests for lib.router — route_by_domains() and route_with_pursuits().

Covers the five required slice-demo cases:
  1. Single customer account match → HIGH
  2. Internal-only domains → is_internal=True
  3. Multi-domain conflict → LOW with both accounts
  4. No match → unknown / NONE
  5. Pursuit keyword match within account

Plus edge cases: mixed internal/external, empty input, full email addresses,
whitespace normalisation, and unknown pursuit_dir.
"""

from pathlib import Path

import pytest

import fieldkit.config._loader as config_module  # alias used in fixtures
from fieldkit.ingest.router import Confidence, RouteResult, route_by_domains, route_with_pursuits

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ACCOUNTS_YAML = """\
internal_domains:
  - internal.example.com
  - staff.example.org

accounts:
  acme-bank:
    domains:
      - acmebank.com
    keywords:
      - "acme"
      - "acmebank"
    pursuit_dir: accounts/acme-bank/pursuits
  shield-insurance:
    domains:
      - shield.example.com
      - shieldins.example.com
    keywords:
      - "shield"
    pursuit_dir: accounts/shield-insurance/pursuits
  global-pay:
    domains:
      - globalpay.io
    keywords:
      - "global pay"
      - "globalpay"
    pursuit_dir: accounts/global-pay/pursuits
"""


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    """Populate a minimal temp data tree used by router tests."""
    # Write accounts.yaml
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "accounts.yaml").write_text(ACCOUNTS_YAML, encoding="utf-8")

    # Write a fieldkit config that points to tmp_path
    cfg_file = tmp_path / "fieldkit.yaml"
    cfg_file.write_text(f"fieldkit_home: {tmp_path}\n", encoding="utf-8")

    # Patch CONFIG_PATH so config helpers resolve to tmp_path
    # monkeypatch is not available in plain fixtures; use direct attribute swap
    # — tests that need monkeypatch do so via the test method signature.

    # acme-bank pursuits
    acme_pursuits = tmp_path / "accounts" / "acme-bank" / "pursuits"
    acme_pursuits.mkdir(parents=True)
    (acme_pursuits / "cloud-migration.md").write_text(
        "---\nstage: qualify\n---\n# Acme Bank — Cloud Migration\nSome body.\n",
        encoding="utf-8",
    )
    (acme_pursuits / "openshift-training.md").write_text(
        "---\nstage: validate\n---\n# Acme Bank — OpenShift Training\nSome body.\n",
        encoding="utf-8",
    )
    (acme_pursuits / "gmail-intel.md").write_text(
        "# Gmail intel (should be skipped)\n",
        encoding="utf-8",
    )
    (acme_pursuits / ".template.md").write_text(
        "# Template (should be skipped)\n",
        encoding="utf-8",
    )

    # shield pursuits
    shield_pursuits = tmp_path / "accounts" / "shield-insurance" / "pursuits"
    shield_pursuits.mkdir(parents=True)
    (shield_pursuits / "ansible-aap.md").write_text(
        "# Shield Insurance — Ansible AAP Rollout\n",
        encoding="utf-8",
    )

    return tmp_path


@pytest.fixture()
def patched_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data_root: Path) -> Path:
    """Patch CONFIG_PATH so all config helpers use the temp accounts.yaml."""
    config_file = tmp_path / "fieldkit.yaml"
    config_file.write_text(f"fieldkit_home: {data_root}\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_file)
    return data_root


# ---------------------------------------------------------------------------
# route_by_domains — case 1: single customer account match
# ---------------------------------------------------------------------------


# ── TestRouteByDomainsSingleMatch (flattened) ───────────────────────────────


def test_get_internal_domains_exact_domain_match_returns_high(patched_config: Path) -> None:
    """Case 1: Single matching customer account → HIGH confidence."""
    result = route_by_domains(["acmebank.com"])

    assert result.accounts == ["acme-bank"]
    assert result.confidence == Confidence.HIGH
    assert result.is_internal is False
    assert result.pursuits == []


def test_get_internal_domains_email_address_extracted_correctly(patched_config: Path) -> None:
    """Full email addresses are reduced to domain for matching."""
    result = route_by_domains(["alice@shield.example.com"])

    assert result.accounts == ["shield-insurance"]
    assert result.confidence == Confidence.HIGH


def test_get_internal_domains_case_insensitive_domain(patched_config: Path) -> None:
    result = route_by_domains(["ACMEBANK.COM"])
    assert result.accounts == ["acme-bank"]
    assert result.confidence == Confidence.HIGH


def test_get_internal_domains_domain_with_whitespace(patched_config: Path) -> None:
    result = route_by_domains(["  globalpay.io  "])
    assert result.accounts == ["global-pay"]


def test_get_internal_domains_multiple_domains_same_account(patched_config: Path) -> None:
    """Multiple domains that all belong to one account → HIGH."""
    result = route_by_domains(["shield.example.com", "shieldins.example.com"])
    assert result.accounts == ["shield-insurance"]
    assert result.confidence == Confidence.HIGH


# ---------------------------------------------------------------------------
# route_by_domains — case 2: internal-only
# ---------------------------------------------------------------------------


# ── TestRouteByDomainsInternal (flattened) ──────────────────────────────────


def test_get_internal_domains_all_internal_returns_generic_fallback(patched_config: Path) -> None:
    """Case 2: all domains are internal and no account is configured."""
    result = route_by_domains(["internal.example.com"])

    assert result.accounts == ["internal"]
    assert result.confidence == Confidence.NONE
    assert result.is_internal is True


def test_get_internal_domains_ibm_domain_is_internal(patched_config: Path) -> None:
    result = route_by_domains(["staff.example.org"])
    assert result.is_internal is True


def test_get_internal_domains_multiple_internal_returns_generic_fallback(patched_config: Path) -> None:
    """Multiple internal domains still use the generic fallback."""
    result = route_by_domains(["internal.example.com", "staff.example.org"])
    assert result.accounts == ["internal"]
    assert result.is_internal is True


def test_route_by_domains_all_internal_resolves_configured_account_slug(
    tmp_path: Path,
) -> None:
    """An all-internal meeting prefers the account explicitly marked internal."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "accounts.yaml").write_text(
        """\
internal_domains:
  - internal.example.com
  - staff.example.org
accounts:
  internal-team:
    internal: true
    domains: []
""",
        encoding="utf-8",
    )

    result = route_by_domains(["internal.example.com"], data_root=tmp_path)

    assert result.accounts == ["internal-team"]
    assert result.confidence == Confidence.NONE
    assert result.is_internal is True


def test_get_internal_domains_mixed_internal_and_external_is_not_internal(patched_config: Path) -> None:
    """Internal domain + external domain → external routing, NOT internal."""
    result = route_by_domains(["internal.example.com", "acmebank.com"])
    assert result.is_internal is False
    assert result.accounts == ["acme-bank"]
    assert result.confidence == Confidence.HIGH


def test_get_internal_domains_email_from_internal_domain(patched_config: Path) -> None:
    result = route_by_domains(["user@internal.example.com"])
    assert result.is_internal is True


# ---------------------------------------------------------------------------
# route_by_domains — case 3: multi-domain conflict → LOW
# ---------------------------------------------------------------------------


# ── TestRouteByDomainsMultiConflict (flattened) ─────────────────────────────


def test_get_internal_domains_two_accounts_returns_low(patched_config: Path) -> None:
    """Case 3: Domains matching two different accounts → LOW confidence."""
    result = route_by_domains(["acmebank.com", "shield.example.com"])

    assert set(result.accounts) == {"acme-bank", "shield-insurance"}
    assert result.confidence == Confidence.LOW
    assert result.is_internal is False


def test_get_internal_domains_three_account_conflict(patched_config: Path) -> None:
    result = route_by_domains(["acmebank.com", "shield.example.com", "globalpay.io"])
    assert result.confidence == Confidence.LOW
    assert len(result.accounts) == 3


# ---------------------------------------------------------------------------
# route_by_domains — case 4: no match → unknown/NONE
# ---------------------------------------------------------------------------


# ── TestRouteByDomainsNoMatch (flattened) ───────────────────────────────────


def test_get_internal_domains_unknown_domain_returns_none(patched_config: Path) -> None:
    """Case 4: Domain not in any account → unknown / NONE."""
    result = route_by_domains(["notanaccount.com"])

    assert result.accounts == ["unknown"]
    assert result.confidence == Confidence.NONE
    assert result.is_internal is False


def test_get_internal_domains_empty_list_returns_unknown(patched_config: Path) -> None:
    result = route_by_domains([])
    assert result.accounts == ["unknown"]
    assert result.confidence == Confidence.NONE


def test_get_internal_domains_empty_strings_are_filtered(patched_config: Path) -> None:
    result = route_by_domains(["", "  "])
    assert result.accounts == ["unknown"]


def test_get_internal_domains_at_sign_only_string(patched_config: Path) -> None:
    """'@' with no domain portion is filtered out."""
    result = route_by_domains(["user@"])
    # "user@" → split on @ last part → "" → filtered
    assert result.accounts == ["unknown"]


# ---------------------------------------------------------------------------
# route_with_pursuits — case 5: pursuit keyword match within account
# ---------------------------------------------------------------------------


# ── TestRouteWithPursuits (flattened) ───────────────────────────────────────


def test_route_with_pursuits_keyword_matches_pursuit_slug(patched_config: Path) -> None:
    """Case 5: Keyword matching finds a pursuit by slug."""
    result = route_with_pursuits(
        ["acmebank.com"],
        keywords=["cloud"],
        data_root=patched_config,
    )

    assert result.accounts == ["acme-bank"]
    assert result.confidence == Confidence.HIGH
    assert "cloud-migration" in result.pursuits


def test_route_with_pursuits_keyword_matches_pursuit_h1(patched_config: Path) -> None:
    """Case 5: Keyword matching finds a pursuit by its H1 title."""
    result = route_with_pursuits(
        ["acmebank.com"],
        keywords=["openshift"],
        data_root=patched_config,
    )
    assert "openshift-training" in result.pursuits


def test_route_with_pursuits_account_keywords_match_without_caller_keywords(patched_config: Path) -> None:
    """Account-level keywords from accounts.yaml are always applied."""
    # shield account has keyword "shield", pursuit h1 is "Shield Insurance — Ansible AAP Rollout"
    result = route_with_pursuits(
        ["shield.example.com"],
        keywords=None,
        data_root=patched_config,
    )
    assert "ansible-aap" in result.pursuits


def test_route_with_pursuits_gmail_intel_file_is_skipped(patched_config: Path) -> None:
    """gmail-intel.md must never appear in pursuit results."""
    result = route_with_pursuits(
        ["acmebank.com"],
        keywords=["gmail"],
        data_root=patched_config,
    )
    assert "gmail-intel" not in result.pursuits


def test_route_with_pursuits_template_file_is_skipped(patched_config: Path) -> None:
    result = route_with_pursuits(
        ["acmebank.com"],
        keywords=["template"],
        data_root=patched_config,
    )
    assert ".template" not in result.pursuits


def test_route_with_pursuits_low_confidence_skips_pursuit_scan(patched_config: Path) -> None:
    """When confidence is LOW (conflict), pursuit matching is not performed."""
    result = route_with_pursuits(
        ["acmebank.com", "shield.example.com"],
        keywords=["cloud"],
        data_root=patched_config,
    )
    assert result.confidence == Confidence.LOW
    assert result.pursuits == []


def test_route_with_pursuits_no_match_skips_pursuit_scan(patched_config: Path) -> None:
    result = route_with_pursuits(
        ["unknown-domain.com"],
        keywords=["cloud"],
        data_root=patched_config,
    )
    assert result.confidence == Confidence.NONE
    assert result.pursuits == []


def test_route_with_pursuits_internal_skips_pursuit_scan(patched_config: Path) -> None:
    result = route_with_pursuits(
        ["internal.example.com"],
        keywords=["cloud"],
        data_root=patched_config,
    )
    assert result.is_internal is True
    assert result.pursuits == []


def test_route_with_pursuits_keyword_no_match_returns_empty_pursuits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keyword that matches no pursuit and no account keyword → pursuits=[].

    Uses an account whose keywords do not appear in any pursuit file,
    so only the caller keywords are tested.
    """
    # Build isolated data root with an account that has no keywords
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "accounts.yaml").write_text(
        """\
internal_domains:
  - internal.example.com
accounts:
  noop-corp:
    domains:
      - noop.com
    keywords: []
    pursuit_dir: accounts/noop-corp/pursuits
""",
        encoding="utf-8",
    )
    pursuits_dir = tmp_path / "accounts" / "noop-corp" / "pursuits"
    pursuits_dir.mkdir(parents=True)
    (pursuits_dir / "alpha.md").write_text("# Noop Corp — Alpha Project\n", encoding="utf-8")

    config_file = tmp_path / "fieldkit.yaml"
    config_file.write_text(f"fieldkit_home: {tmp_path}\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_file)

    result = route_with_pursuits(
        ["noop.com"],
        keywords=["zzznomatch"],
        data_root=tmp_path,
    )
    assert result.pursuits == []


def test_route_with_pursuits_multiple_keywords_union_match(patched_config: Path) -> None:
    """Multiple keywords: pursuits matching any of them are included."""
    result = route_with_pursuits(
        ["acmebank.com"],
        keywords=["cloud", "openshift"],
        data_root=patched_config,
    )
    assert "cloud-migration" in result.pursuits
    assert "openshift-training" in result.pursuits


# ---------------------------------------------------------------------------
# RouteResult dataclass
# ---------------------------------------------------------------------------


# ── TestRouteResult (flattened) ─────────────────────────────────────────────


def test_route_result_frozen() -> None:
    r = RouteResult(accounts=["acme"], confidence=Confidence.HIGH, is_internal=False)
    with pytest.raises((TypeError, AttributeError)) as exc_info:
        r.accounts = ["other"]  # type: ignore[misc]
    assert issubclass(exc_info.type, (TypeError, AttributeError))


def test_route_result_pursuits_default_empty() -> None:
    r = RouteResult(accounts=["acme"], confidence=Confidence.HIGH, is_internal=False)
    assert r.pursuits == []


def test_route_result_pursuits_not_shared() -> None:
    """Two instances created with default pursuits should not share the same list."""
    r1 = RouteResult(accounts=["a"], confidence=Confidence.HIGH, is_internal=False)
    r2 = RouteResult(accounts=["b"], confidence=Confidence.HIGH, is_internal=False)
    assert r1.pursuits is not r2.pursuits


# ---------------------------------------------------------------------------
# Confidence enum
# ---------------------------------------------------------------------------


# ── TestConfidence (flattened) ──────────────────────────────────────────────


def test_confidence_is_str() -> None:
    assert isinstance(Confidence.HIGH, str)
    assert Confidence.HIGH == "high"


def test_confidence_values() -> None:
    assert {c.value for c in Confidence} == {"high", "low", "none"}
