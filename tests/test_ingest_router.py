"""Parameterized tests for ingest routing alias tables.

historic regression: 40+ routing aliases in ingest/router.py had no systematic test.
A typo in any alias goes undetected until a transcript fails to route.

Two alias tables are covered:
1. _PRODUCT_ALIASES in src/fieldkit/ingest/router.py — product name normalization
   (spoken/transcription variants → canonical slug fragment)
2. _ACCOUNT_NAME_ALIASES in src/fieldkit/commands/ingest/route.py — filename
   prefix aliases (e.g. 'b-of-a' → 'acme-corp')  # pii-guard: ignore

Tests are data-driven: if either alias table grows, tests grow automatically
by importing the table directly and iterating over it.
"""

import pytest

from fieldkit.ingest.route_batch import _ACCOUNT_NAME_ALIASES, _apply_account_aliases
from fieldkit.ingest.router import _PRODUCT_ALIASES, _normalize_pursuit_keywords

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Story 3a: _PRODUCT_ALIASES — product name normalization
# ---------------------------------------------------------------------------
# Spot-check the most important aliases to ensure the table is correct.
# Full coverage is provided by the data-driven test below.

_PRODUCT_ALIAS_SPOT_CHECKS: list[tuple[str, str]] = [
    # RHOAI variants — all normalize correctly
    ("rhoai", "rhoai"),
    ("roai", "rhoai"),
    ("ro ai", "rhoai"),
    ("rho ai", "rhoai"),
    ("r-oai", "rhoai"),
    ("roy ai", "rhoai"),
    ("row ai", "rhoai"),
    ("openshift ai", "rhoai"),
    ("open shift ai", "rhoai"),
    ("openshift data science", "rhoai"),
    # AAP variants
    ("aap", "aap"),
    ("ansible automation platform", "aap"),
    # EDA variants
    ("eda", "eda"),
    ("event driven ansible", "eda"),
    ("event-driven ansible", "eda"),
    # AEP variants
    ("aep", "aep"),
    ("automation execution platform", "aep"),
    ("ansible execution platform", "aep"),
    # RHOCP variants
    ("rhocp", "rhocp"),
    ("ocp", "rhocp"),
    ("openshift container platform", "rhocp"),
    ("openshift enterprise", "rhocp"),
    # OCP Virt
    ("ocp virt", "ocp-virt"),
    ("openshift virt", "ocp-virt"),
    ("openshift virtualization", "ocp-virt"),
    ("openshift virtualization engine", "ocp-virt"),
    # RHEL
    ("rhel", "rhel"),
    # RHEL AI
    ("rhel ai", "rhel-ai"),
    # RHAIE
    ("rhaie", "rhaie"),
    ("ai enterprise", "rhaie"),
    # ACM
    ("acm", "acm"),
    ("advanced cluster management", "acm"),
    # ACS
    ("acs", "acs"),
    ("advanced cluster security", "acs"),
    # ODF
    ("odf", "odf"),
    ("openshift data foundation", "odf"),
    # RHOSO
    ("rhoso", "rhoso"),
    ("openstack services on openshift", "rhoso"),
    # RHDH
    ("rhdh", "rhdh"),
    ("developer hub", "rhdh"),
    # RHTAS
    ("rhtas", "rhtas"),
    ("trusted artifact signer", "rhtas"),
    ("trusted signer", "rhtas"),
    # RHTPA
    ("rhtpa", "rhtpa"),
    ("trusted profile analyzer", "rhtpa"),
    ("trusted content", "rhtpa"),
    # EAP
    ("eap", "eap"),
    ("jboss eap", "eap"),
    ("jboss", "eap"),
    # Quay
    ("quay", "quay"),
    # Service Interconnect
    ("service interconnect", "interconnect"),
    ("application interconnect", "interconnect"),
]


@pytest.mark.parametrize("alias,expected_canonical", _PRODUCT_ALIAS_SPOT_CHECKS)
def test_product_alias_maps_to_canonical(alias: str, expected_canonical: str) -> None:
    """Every documented product alias maps to the correct canonical slug fragment.

    Uses _normalize_pursuit_keywords() which applies _PRODUCT_ALIASES internally.
    The alias is passed as a single keyword; the canonical slug must appear in the result.
    """
    result = _normalize_pursuit_keywords([alias])
    assert expected_canonical in result, f"Alias {alias!r} should normalize to {expected_canonical!r}, got {result!r}"


def test_product_alias_table_has_expected_size() -> None:
    """_PRODUCT_ALIASES has at least 40 entries (spec requirement: '40+ routing aliases')."""
    assert len(_PRODUCT_ALIASES) >= 40, f"Expected 40+ product aliases, got {len(_PRODUCT_ALIASES)}"


# ---------------------------------------------------------------------------
# Story 3b: _ACCOUNT_NAME_ALIASES — filename prefix normalization
# ---------------------------------------------------------------------------

_ACCOUNT_ALIAS_SPOT_CHECKS: list[tuple[str, str]] = [
    # b-of-a → acme-corp (historic regression)  # pii-guard: ignore
    ("b-of-a-2026-06-08-meeting", "acme-corp-2026-06-08-meeting"),  # pii-guard: ignore
    ("b-of-a_2026-06-08-meeting", "acme-corp_2026-06-08-meeting"),  # pii-guard: ignore
    # bank-of-america → acme-corp  # pii-guard: ignore
    ("bank-of-america-2026-06-08-meeting", "acme-corp-2026-06-08-meeting"),  # pii-guard: ignore
    ("bank-of-america_2026-06-08-meeting", "acme-corp_2026-06-08-meeting"),  # pii-guard: ignore
]

_ACCOUNT_ALIAS_UNCHANGED: list[str] = [
    "acme-corp-2026-05-01-kickoff",
    "globalpay-2026-05-01-q1",
    "midwestins-2026-05-01-review",
    "client-2026-05-01-meeting",
    "company-2026-05-01-sync",
    "2026-05-01-no-prefix",
]


@pytest.mark.parametrize("stem,expected_normalized", _ACCOUNT_ALIAS_SPOT_CHECKS)
def test_account_alias_normalizes_stem(stem: str, expected_normalized: str) -> None:
    """Every documented account name alias normalizes the filename stem correctly."""
    result = _apply_account_aliases(stem)
    assert result == expected_normalized, (
        f"_apply_account_aliases({stem!r}) should return {expected_normalized!r}, got {result!r}"
    )


@pytest.mark.parametrize("stem", _ACCOUNT_ALIAS_UNCHANGED)
def test_account_alias_leaves_normal_stems_unchanged(stem: str) -> None:
    """_apply_account_aliases does not modify stems without known aliases."""
    result = _apply_account_aliases(stem)
    assert result == stem, f"_apply_account_aliases({stem!r}) should return unchanged {stem!r}, got {result!r}"


def test_account_alias_table_is_complete() -> None:
    """All entries in _ACCOUNT_NAME_ALIASES are covered by spot-check list."""
    # Each alias in the table should appear in at least one spot-check stem
    for alias in _ACCOUNT_NAME_ALIASES:
        found = any(alias + "-" in stem or alias + "_" in stem for stem, _ in _ACCOUNT_ALIAS_SPOT_CHECKS)
        assert found, (
            f"Account alias {alias!r} from _ACCOUNT_NAME_ALIASES not covered by spot checks.\n"
            "Add entries to _ACCOUNT_ALIAS_SPOT_CHECKS in test_ingest_router.py."
        )


# ---------------------------------------------------------------------------
# Data-driven: iterate over full _PRODUCT_ALIASES table automatically
# ---------------------------------------------------------------------------


def test_all_product_aliases_normalize_to_their_canonical() -> None:
    """Every entry in _PRODUCT_ALIASES normalizes to its canonical value.

    This is the data-driven complement to the spot-check parametrize above.
    If the alias table grows, this test automatically covers new entries.
    """
    failures: list[str] = []
    for alias, expected_canonical in _PRODUCT_ALIASES.items():
        result = _normalize_pursuit_keywords([alias])
        if expected_canonical not in result:
            failures.append(f"  {alias!r} → expected {expected_canonical!r}, got {result!r}")

    assert not failures, "Product alias normalization failures:\n" + "\n".join(failures)


def test_all_account_aliases_normalize_correctly() -> None:
    """Every entry in _ACCOUNT_NAME_ALIASES normalizes a stem with that prefix."""
    failures: list[str] = []
    for alias, canonical in _ACCOUNT_NAME_ALIASES.items():
        stem = f"{alias}-2026-01-01-meeting"
        expected = f"{canonical}-2026-01-01-meeting"
        result = _apply_account_aliases(stem)
        if result != expected:
            failures.append(f"  {stem!r} → expected {expected!r}, got {result!r}")

    assert not failures, "Account alias normalization failures:\n" + "\n".join(failures)
