"""Regression tests: internal-domain list is picked up at call time.

T02 (M003/S03) verifies that the five gmail_cache modules read
get_internal_domains() on every call rather than freezing the value at
import time.  Patching get_internal_domains in each module's namespace
must be reflected immediately — no module reload required.

Patch target convention: because each module does
    from fieldkit.config import get_internal_domains
the correct monkeypatch target is the name *in that module's namespace*,
e.g. ``fieldkit.commands.gmail.decay.get_internal_domains``.
Patching ``lib.config.get_internal_domains`` alone would not work because
the module already holds a direct reference to the original function.
"""

import pytest

from fieldkit.commands.gmail.apply_intel import _internal_domains
from fieldkit.gmail.decay_domain import is_internal_or_noise
from fieldkit.gmail.query_domain import _is_noise

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# decay.is_internal_or_noise
# ---------------------------------------------------------------------------


# ── TestDecayDomainAtCallTime (flattened) ───────────────────────────────────


def test_decay_domain_explicit_internal_domain_is_filtered() -> None:
    assert is_internal_or_noise("user@internal.example.com", internal_domains=("internal.example.com",)) is True


def test_decay_domain_external_domain_is_not_filtered() -> None:
    assert is_internal_or_noise("user@external.example.com", internal_domains=("internal.example.com",)) is False


def test_decay_domain_empty_internal_scope_still_filters_automation(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty list means no internal domains — external addresses are not filtered.
    org-agnostic-config: fallback to hardcoded org domains removed (task 1.3).
    AUTOMATION_NOISE_DOMAINS still filters automation noise regardless of org config."""
    import fieldkit.gmail.decay_domain as decay_domain

    monkeypatch.setattr(decay_domain, "AUTOMATION_NOISE_DOMAINS", frozenset({"docusign.example.com"}))

    # External address — not filtered when no internal domains configured
    assert is_internal_or_noise("cfo@globalpay.example.com", internal_domains=()) is False
    # Automation noise — still filtered via AUTOMATION_NOISE_DOMAINS (D2 split)
    assert is_internal_or_noise("noreply@docusign.example.com", internal_domains=()) is True


def test_decay_domain_scope_changes_without_module_state() -> None:
    assert is_internal_or_noise("boss@acme-corp.example.com", internal_domains=("acme-corp.example.com",)) is True
    assert is_internal_or_noise("boss@acme-corp.example.com", internal_domains=()) is False


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("employee@internal.example.com", True),
        ("employee@dept.internal.example.com", True),
        ("employee@evilinternal.example.com", False),
        ("employee@internal.example.com.evil.example", False),
    ],
)
def test_decay_domain_internal_boundary_matches_exact_domains_and_subdomains(
    email: str,
    expected: bool,
) -> None:
    assert is_internal_or_noise(email, internal_domains=("internal.example.com",)) is expected


# ---------------------------------------------------------------------------
# _is_noise  (uses _internal_blind_domains())
# ---------------------------------------------------------------------------


# ── TestQueryDomainAtCallTime (flattened) ───────────────────────────────────


def test_query_domain_at_call_time_novel_domain_classified_noise_after_patch(monkeypatch):
    import fieldkit.gmail.query_domain as query

    monkeypatch.setattr(query, "get_internal_domains", lambda: ["internal.example.com"])

    # _is_noise returns True for addresses in the internal-domain set
    assert _is_noise("user@internal.example.com") is True


def test_query_domain_at_call_time_removed_domain_no_longer_noise(monkeypatch):
    import fieldkit.gmail.query_domain as query

    monkeypatch.setattr(query, "get_internal_domains", lambda: ["example-internal.com"])

    # The external fixture domain is not in the patched list and has no noise prefix.
    assert _is_noise("alice@external.example.com") is False


# ---------------------------------------------------------------------------
# _internal_domains()
# ---------------------------------------------------------------------------


# ── TestApplyIntelDomainAtCallTime (flattened) ──────────────────────────────


def test_apply_intel_domain_at_call_time_returns_patched_list(monkeypatch):
    import fieldkit.commands.gmail.apply_intel as apply_intel

    monkeypatch.setattr(apply_intel, "get_internal_domains", lambda: ["custom-internal.net"])

    result = _internal_domains()
    assert result == ["custom-internal.net"]


def test_apply_intel_domain_at_call_time_returns_empty_when_not_configured(monkeypatch):
    """org-agnostic-config: fallback to hardcoded org domains removed (task 1.1).
    Empty internal_domains config → empty list returned."""
    import fieldkit.commands.gmail.apply_intel as apply_intel

    monkeypatch.setattr(apply_intel, "get_internal_domains", lambda: [])

    result = _internal_domains()
    assert result == []


def test_apply_intel_domain_at_call_time_no_module_reload_needed(monkeypatch):
    import fieldkit.commands.gmail.apply_intel as m  # module already imported

    monkeypatch.setattr(m, "get_internal_domains", lambda: ["live-patch.io"])

    assert "live-patch.io" in m._internal_domains()
