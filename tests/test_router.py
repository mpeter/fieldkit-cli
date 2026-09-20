"""Slice S03 comprehensive pytest suite for lib.router.

Covers the five required slice-demo cases with real-world account names
(acme-bank, globalpay, midwest-ins) and a full edge-case battery.

Required cases (per slice success criteria):
  1. test_single_customer_match — globalpay.example.com → accounts=['globalpay'], HIGH
  2. test_internal_only_routes_to_generic_fallback → is_internal=True
  3. test_multi_domain_conflict_low_confidence — globalpay+acme-bank → LOW, both accounts
  4. test_no_match_returns_unknown — acme.example.com → unknown, NONE
  5. test_pursuit_keyword_match_within_account — acme-bank + 'ads' → contains 'ads-repave-services'

Edge cases:
  - Mixed internal + customer → external routing, HIGH
  - Empty domain list → unknown
  - Domain casing normalised → matches globalpay
  - Full email address → domain extracted correctly
  - route_with_pursuits no keywords → empty pursuits
  - Pursuit matched by H1 heading text
  - All-whitespace domain strings → treated as empty
"""

from pathlib import Path

import pytest

import fieldkit.config._loader as config_module  # alias used in fixtures
from fieldkit.ingest.router import (
    Confidence,
    _normalize_pursuit_keywords,
    route_by_domains,
    route_by_title,
    route_with_pursuits,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

ACCOUNTS_YAML = """\
internal_domains:
  - internal.example.com
  - staff.example.org

accounts:
  globalpay:
    domains:
      - globalpay.example.com
    keywords:
      - "globalpay"
    pursuit_dir: accounts/globalpay/pursuits

  acme-bank:
    domains:
      - acmebank.example.com
      - acme-corp.example.com
      - acme-ml.example.com
    keywords:
      - "acme-bank"
      - "acme bank"
      - "acmebank"
    pursuit_dir: accounts/acme-bank/pursuits

  midwest-ins:
    domains:
      - midwestins.example.com
    keywords:
      - "midwest ins"
      - "midwest insurance"
      - "midwestins"
    pursuit_dir: accounts/midwest-ins/pursuits
"""


@pytest.fixture()
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a minimal temp data tree and monkeypatch CONFIG_PATH to use it.

    Structure:
      <tmp>/config/accounts.yaml          — 3 accounts + internal_domains
      <tmp>/accounts/internal-team/account.md    — internal_only: true
      <tmp>/accounts/acme-bank/pursuits/ads-repave-services.md
      <tmp>/accounts/globalpay/pursuits/drift-management.md
      <tmp>/accounts/midwest-ins/pursuits/rhel-renewal.md
    """
    # Write accounts.yaml
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "accounts.yaml").write_text(ACCOUNTS_YAML, encoding="utf-8")

    # Write fieldkit.yaml so get_fieldkit_home() resolves to tmp_path
    config_file = tmp_path / "fieldkit.yaml"
    config_file.write_text(f"fieldkit_home: {tmp_path}\n", encoding="utf-8")

    # Monkeypatch CONFIG_PATH so all config helpers read the temp config
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_file)

    # internal-team/account.md — internal_only account file
    internal_dir = tmp_path / "accounts" / "internal-team"
    internal_dir.mkdir(parents=True)
    (internal_dir / "account.md").write_text(
        "---\ninternal_only: true\n---\n# Internal Team\n",
        encoding="utf-8",
    )

    # acme-bank pursuits
    acme_bank_pursuits = tmp_path / "accounts" / "acme-bank" / "pursuits"
    acme_bank_pursuits.mkdir(parents=True)
    (acme_bank_pursuits / "ads-repave-services.md").write_text(
        "---\nstage: qualify\n---\n# ADS Repave Services\nMigrate ADS workloads to OpenShift.\n",
        encoding="utf-8",
    )

    # globalpay pursuits
    globalpay_pursuits = tmp_path / "accounts" / "globalpay" / "pursuits"
    globalpay_pursuits.mkdir(parents=True)
    (globalpay_pursuits / "drift-management.md").write_text(
        "---\nstage: validate\n---\n# Drift Management Services\nAnsible drift remediation.\n",
        encoding="utf-8",
    )

    # midwest-ins pursuits
    sf_pursuits = tmp_path / "accounts" / "midwest-ins" / "pursuits"
    sf_pursuits.mkdir(parents=True)
    (sf_pursuits / "rhel-renewal.md").write_text(
        "---\nstage: qualify\n---\n# RHEL Subscription Renewal\nAnnual renewal.\n",
        encoding="utf-8",
    )

    return tmp_path


# ---------------------------------------------------------------------------
# Required case 1: single customer account match
# ---------------------------------------------------------------------------


def test_single_customer_match(data_root: Path) -> None:
    """Case 1: globalpay.example.com → accounts=['globalpay'], confidence=HIGH, is_internal=False."""
    result = route_by_domains(["alice@globalpay.example.com"])

    assert result.accounts == ["globalpay"]
    assert result.confidence == Confidence.HIGH
    assert result.is_internal is False
    assert result.pursuits == []


# ---------------------------------------------------------------------------
# Required case 2: internal-only routes to the generic fallback
# ---------------------------------------------------------------------------


def test_internal_only_routes_to_generic_fallback(data_root: Path) -> None:
    """Case 2: configured internal domains use a generic fallback account."""
    result = route_by_domains(["alice@internal.example.com", "bob@staff.example.org"])

    assert result.accounts == ["internal"]
    assert result.confidence == Confidence.NONE
    assert result.is_internal is True


# ---------------------------------------------------------------------------
# Required case 3: multi-domain conflict → LOW confidence
# ---------------------------------------------------------------------------


def test_multi_domain_conflict_low_confidence(data_root: Path) -> None:
    """Case 3: globalpay.example.com + acmebank.example.com → LOW, both accounts returned."""
    result = route_by_domains(["alice@globalpay.example.com", "bob@acmebank.example.com"])

    assert result.confidence == Confidence.LOW
    assert set(result.accounts) == {"globalpay", "acme-bank"}
    assert result.is_internal is False


# ---------------------------------------------------------------------------
# Required case 4: no match → unknown / NONE
# ---------------------------------------------------------------------------


def test_no_match_returns_unknown(data_root: Path) -> None:
    """Case 4: acme.example.com matches no configured account → unknown, NONE."""
    result = route_by_domains(["nobody@acme.example.com"])

    assert result.accounts == ["unknown"]
    assert result.confidence == Confidence.NONE
    assert result.is_internal is False


# ---------------------------------------------------------------------------
# Required case 5: pursuit keyword match within account
# ---------------------------------------------------------------------------


def test_pursuit_keyword_match_within_account(data_root: Path) -> None:
    """Case 5: acme-bank + keyword 'ads' → pursuits contains 'ads-repave-services'."""
    result = route_with_pursuits(
        ["alice@acmebank.example.com"],
        keywords=["ads"],
        data_root=data_root,
    )

    assert result.accounts == ["acme-bank"]
    assert result.confidence == Confidence.HIGH
    assert "ads-repave-services" in result.pursuits


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_mixed_internal_and_customer(data_root: Path) -> None:
    """Mixed internal and customer domains route to the customer with high confidence."""
    result = route_by_domains(["user@internal.example.com", "john@globalpay.example.com"])

    assert result.accounts == ["globalpay"]
    assert result.confidence == Confidence.HIGH
    assert result.is_internal is False


def test_empty_domain_list(data_root: Path) -> None:
    """Empty list → unknown, NONE, not internal."""
    result = route_by_domains([])

    assert result.accounts == ["unknown"]
    assert result.confidence == Confidence.NONE
    assert result.is_internal is False


def test_domain_casing_normalized(data_root: Path) -> None:
    """Uppercase domain in email address is lowercased before matching."""
    result = route_by_domains(["ALICE@GLOBALPAY.EXAMPLE.COM"])

    assert result.accounts == ["globalpay"]
    assert result.confidence == Confidence.HIGH


def test_full_email_address_handled(data_root: Path) -> None:
    """Full email address format → domain extracted correctly → matches account."""
    result = route_by_domains(["alice@globalpay.example.com"])

    assert result.accounts == ["globalpay"]
    assert result.confidence == Confidence.HIGH


def test_route_with_pursuits_no_keywords(data_root: Path) -> None:
    """route_with_pursuits() without caller keywords uses only account keywords.

    globalpay's account keywords contain 'globalpay'; the drift-management.md H1 is
    'Drift Management Services', which does NOT contain 'globalpay'. No account
    keyword matches → pursuits is empty.
    """
    # globalpay account keywords: ['globalpay'] — none of the globalpay pursuit files
    # contain 'globalpay' in their slug or H1, so pursuits should be empty.
    result = route_with_pursuits(
        ["alice@globalpay.example.com"],
        data_root=data_root,
    )

    assert result.accounts == ["globalpay"]
    assert result.confidence == Confidence.HIGH
    # Account keyword 'globalpay' does not appear in 'drift-management' slug or
    # 'Drift Management Services' H1 → no match
    assert result.pursuits == []


def test_pursuit_match_by_h1_heading(data_root: Path) -> None:
    """Keyword matching works against the H1 heading text, not just the file slug."""
    # 'repave' appears only in the H1 'ADS Repave Services', not in the slug
    # 'ads-repave-services' — but the combined haystack is 'ads-repave-services ads repave services'
    # which still contains 'repave'. Use a word unique to the H1 portion only.
    result = route_with_pursuits(
        ["alice@acmebank.example.com"],
        keywords=["repave services"],  # phrase only in H1, not in slug
        data_root=data_root,
    )

    assert "ads-repave-services" in result.pursuits


def test_whitespace_only_domains_treated_as_empty(data_root: Path) -> None:
    """All-whitespace domain strings are stripped → empty → unknown."""
    result = route_by_domains(["   ", "\t", ""])

    assert result.accounts == ["unknown"]
    assert result.confidence == Confidence.NONE


def test_acme_alias_domain_matches(data_root: Path) -> None:
    """acme-corp.example.com alias (not primary domain) also matches the acme-bank account."""
    result = route_by_domains(["alice@acme-corp.example.com"])

    assert result.accounts == ["acme-bank"]
    assert result.confidence == Confidence.HIGH


def test_ml_alias_domain_matches(data_root: Path) -> None:
    """acme-ml.example.com alias also maps to acme-bank account."""
    result = route_by_domains(["alice@acme-ml.example.com"])

    assert result.accounts == ["acme-bank"]
    assert result.confidence == Confidence.HIGH


def test_state_farm_domain_matches(data_root: Path) -> None:
    """midwestins.example.com → accounts=['midwest-ins'], HIGH."""
    result = route_by_domains(["agent@midwestins.example.com"])

    assert result.accounts == ["midwest-ins"]
    assert result.confidence == Confidence.HIGH


def test_pursuit_match_state_farm(data_root: Path) -> None:
    """Midwest Ins pursuit matched by account keyword 'midwestins'."""
    # rhel-renewal.md slug contains 'rhel' and H1 contains 'RHEL Subscription Renewal'
    result = route_with_pursuits(
        ["agent@midwestins.example.com"],
        keywords=["rhel"],
        data_root=data_root,
    )

    assert "rhel-renewal" in result.pursuits


# ---------------------------------------------------------------------------
# route_by_title tests
# ---------------------------------------------------------------------------


def test_route_by_title_acme_bank_match(data_root: Path) -> None:
    """'AcmeBank AI Weekly Connect' → accounts=['acme-bank'], LOW confidence."""
    result = route_by_title(
        "Rh AcmeBank AI Weekly Connect - 2026/04/22 14:00 EDT - Notes by Gemini", data_root=data_root
    )
    assert result.accounts == ["acme-bank"]
    assert result.confidence == Confidence.LOW
    assert result.is_internal is False


def test_route_by_title_globalpay_match(data_root: Path) -> None:
    """'GlobalPay - Weekly Delivery' → accounts=['globalpay'], LOW confidence."""
    result = route_by_title("GlobalPay - Weekly Delivery & Account Touchpoint - 2026/05/06", data_root=data_root)
    assert result.accounts == ["globalpay"]
    assert result.confidence == Confidence.LOW


def test_route_by_title_state_farm_match(data_root: Path) -> None:
    """'Midwest Insurance ROSA' → accounts=['midwest-ins'], LOW confidence."""
    result = route_by_title("Midwest Insurance Internal - ROSA 2.0 - Notes by Gemini", data_root=data_root)
    assert result.accounts == ["midwest-ins"]
    assert result.confidence == Confidence.LOW


def test_route_by_title_case_insensitive(data_root: Path) -> None:
    """Title matching is case-insensitive."""
    result = route_by_title("ACMEBANK INTERNAL SYNC", data_root=data_root)
    assert result.accounts == ["acme-bank"]
    assert result.confidence == Confidence.LOW


def test_route_by_title_short_keyword_does_not_substring_match(data_root: Path) -> None:
    """historic regression: a keyword like 'ai' must not match inside an unrelated word like 'daily'."""
    result = route_by_title("Daily Standup - Internal Sync", data_root=data_root)
    assert result.accounts == ["unknown"]
    assert result.confidence == Confidence.NONE


def test_route_by_title_word_boundary_still_matches_whole_word(data_root: Path) -> None:
    """historic regression regression guard: a whole-word keyword still matches after the fix."""
    result = route_by_title("ACMEBANK INTERNAL SYNC", data_root=data_root)
    assert "acme-bank" in result.accounts


def test_route_by_title_no_match_returns_unknown(data_root: Path) -> None:
    """Title with no account keywords → unknown, NONE."""
    result = route_by_title("Weekly Engineering Sync - Internal Only")
    assert result.accounts == ["unknown"]
    assert result.confidence == Confidence.NONE


def test_route_by_title_empty_returns_unknown(data_root: Path) -> None:
    """Empty title → unknown."""
    result = route_by_title("")
    assert result.accounts == ["unknown"]
    assert result.confidence == Confidence.NONE


def test_route_by_title_multi_match_low_confidence(data_root: Path) -> None:
    """Title matching multiple accounts → LOW, all matched accounts."""
    # "globalpay" matches globalpay account; "acme bank" matches acme-bank account
    result = route_by_title("GlobalPay and Acme Bank joint discussion", data_root=data_root)
    assert set(result.accounts) == {"globalpay", "acme-bank"}
    assert result.confidence == Confidence.LOW


def test_conflict_includes_all_matched_accounts(data_root: Path) -> None:
    """Three-way conflict → LOW confidence and all three account names returned."""
    result = route_by_domains(["a@globalpay.example.com", "b@acme-corp.example.com", "c@midwestins.example.com"])

    assert result.confidence == Confidence.LOW
    assert set(result.accounts) == {"globalpay", "acme-bank", "midwest-ins"}


def test_no_pursuit_scan_on_low_confidence(data_root: Path) -> None:
    """LOW confidence (conflict) → pursuit scan skipped → pursuits=[]."""
    result = route_with_pursuits(
        ["alice@globalpay.example.com", "bob@acmebank.example.com"],
        keywords=["ads"],
        data_root=data_root,
    )

    assert result.confidence == Confidence.LOW
    assert result.pursuits == []


def test_no_pursuit_scan_on_internal(data_root: Path) -> None:
    """Internal route → pursuit scan skipped → pursuits=[]."""
    result = route_with_pursuits(
        ["alice@internal.example.com"],
        keywords=["ads"],
        data_root=data_root,
    )

    assert result.is_internal is True
    assert result.pursuits == []


# ---------------------------------------------------------------------------
# Regression: data_root provides full config isolation (S06)
# ---------------------------------------------------------------------------
#
# These tests verify that passing data_root causes route_* functions to read
# accounts.yaml from data_root/config/accounts.yaml (full isolation) rather
# than from the real install config (partial/false isolation).
#
# The key invariant: 'fixture-domain.com' must NOT exist in the real install's
# accounts.yaml.  If it ever does, the "no data_root" assertions below will
# wrongly fail — that would itself be a bug worth catching.
# ---------------------------------------------------------------------------

FIXTURE_ACCOUNTS_YAML = """\
internal_domains:
  - internal.example.com

accounts:
  fixture-co:
    domains:
      - fixture-domain.com
    keywords:
      - "fixture"
    pursuit_dir: accounts/fixture-co/pursuits
"""


@pytest.fixture()
def fixture_data_root(tmp_path: Path) -> Path:
    """Minimal isolated data root with a single 'fixture-co' account.

    Does NOT monkeypatch CONFIG_PATH — the isolation comes entirely from
    passing data_root to the router functions, not from patching globals.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "accounts.yaml").write_text(FIXTURE_ACCOUNTS_YAML, encoding="utf-8")

    pursuits_dir = tmp_path / "accounts" / "fixture-co" / "pursuits"
    pursuits_dir.mkdir(parents=True)
    (pursuits_dir / "fixture-pursuit.md").write_text(
        "---\nstage: qualify\n---\n# Fixture Pursuit — Demo Engagement\nDemo body.\n",
        encoding="utf-8",
    )

    return tmp_path


def test_data_root_isolation_route_by_domains_with_data_root(
    fixture_data_root: Path,
) -> None:
    """Passing data_root makes route_by_domains read the fixture accounts.yaml.

    fixture-domain.com is only known to the fixture config; with data_root
    supplied the router must return accounts=['fixture-co'].
    """
    result = route_by_domains(["fixture-domain.com"], data_root=fixture_data_root)

    assert result.accounts == ["fixture-co"]
    assert result.confidence == Confidence.HIGH
    assert result.is_internal is False


def test_data_root_isolation_route_by_domains_without_data_root() -> None:
    """Without data_root, route_by_domains reads the real install config.

    fixture-domain.com is NOT in the real accounts.yaml, so the result must
    be unknown/NONE — proving that with-data_root and without-data_root behave
    differently for this domain (the full-isolation guarantee).
    """
    result = route_by_domains(["fixture-domain.com"])

    assert result.accounts == ["unknown"]
    assert result.confidence == Confidence.NONE


def test_data_root_isolation_route_with_pursuits(
    fixture_data_root: Path,
) -> None:
    """Passing data_root causes route_with_pursuits to use the fixture config.

    Both domain routing and pursuit scanning must resolve through the fixture
    data_root — no real config or real pursuit directory is accessed.
    """
    result = route_with_pursuits(
        ["fixture-domain.com"],
        keywords=["demo"],
        data_root=fixture_data_root,
    )

    assert result.accounts == ["fixture-co"]
    assert result.confidence == Confidence.HIGH
    assert "fixture-pursuit" in result.pursuits


def test_data_root_isolation_route_with_pursuits_account_keywords(
    fixture_data_root: Path,
) -> None:
    """Account keywords from the fixture accounts.yaml are used for pursuit matching.

    The fixture account has keyword 'fixture'; the pursuit slug 'fixture-pursuit'
    contains that keyword, so the pursuit must be returned even without caller keywords.
    """
    result = route_with_pursuits(
        ["fixture-domain.com"],
        data_root=fixture_data_root,
    )

    assert result.accounts == ["fixture-co"]
    assert "fixture-pursuit" in result.pursuits


# ---------------------------------------------------------------------------
# Unit tests for _normalize_pursuit_keywords (historic regression)
# ---------------------------------------------------------------------------


def test_normalize_stopwords_filtered() -> None:
    """Generic stopwords like 'security' and 'production' are filtered out."""
    result = _normalize_pursuit_keywords(["security production platform"])
    assert "security" not in result
    assert "production" not in result
    assert "platform" not in result


def test_normalize_short_tokens_dropped() -> None:
    """Tokens shorter than 3 characters are dropped."""
    result = _normalize_pursuit_keywords(["ai ml to do"])
    assert "ai" not in result
    assert "ml" not in result
    assert "to" not in result
    assert "do" not in result


def test_normalize_product_alias_roai_to_rhoai() -> None:
    """'RoAI' variant is canonicalized to 'rhoai'."""
    result = _normalize_pursuit_keywords(["RoAI"])
    assert result == ["rhoai"]


def test_normalize_product_names_survive() -> None:
    """Specific product names like 'rhoai', 'ansible', 'openshift' survive normalization."""
    result = _normalize_pursuit_keywords(["rhoai ansible openshift"])
    assert "rhoai" in result
    assert "ansible" in result
    assert "openshift" in result


def test_normalize_aap_survives() -> None:
    """'aap' (3 chars, product alias) survives despite being at the length boundary."""
    result = _normalize_pursuit_keywords(["aap"])
    assert "aap" in result


def test_normalize_multi_word_tokenized() -> None:
    """Multi-word input is tokenized and each token filtered independently."""
    result = _normalize_pursuit_keywords(["rhoai production security"])
    assert "rhoai" in result
    assert "production" not in result
    assert "security" not in result


def test_normalize_deduplication() -> None:
    """Repeated tokens appear only once in the output."""
    result = _normalize_pursuit_keywords(["rhoai", "rhoai"])
    assert result.count("rhoai") == 1


def test_normalize_empty_input_returns_empty() -> None:
    """Empty input returns an empty list."""
    assert _normalize_pursuit_keywords([]) == []


def test_normalize_mixed_case_lowercased() -> None:
    """Mixed case is normalized to lowercase in output."""
    result = _normalize_pursuit_keywords(["OpenShift"])
    assert "openshift" in result
    assert "OpenShift" not in result


def test_normalize_punctuation_stripped() -> None:
    """Punctuation at token edges is stripped before filtering."""
    result = _normalize_pursuit_keywords(["rhoai,"])
    assert "rhoai" in result
    assert "rhoai," not in result


def test_normalize_stopwords_with_product_only_meaningful() -> None:
    """A phrase with stopwords and a product name yields only the product name."""
    result = _normalize_pursuit_keywords(["rhoai production platform security"])
    assert result == ["rhoai"]
