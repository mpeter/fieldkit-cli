"""Shared fail-closed support for bounded Gmail readers."""

from __future__ import annotations

import sqlite3

import pytest

from fieldkit.config import ConfigError
from fieldkit.gmail.query_support import (
    MAX_CONFIGURED_ACCOUNTS,
    MAX_DOMAINS_PER_ACCOUNT,
    configured_account_scope,
    domain_matches_boundary,
    scan_row_budget,
    sqlite_query_budget,
)

pytestmark = pytest.mark.unit


def test_backstory_gap_does_not_reexport_shared_query_support() -> None:
    from fieldkit.gmail import backstory_gap

    assert "scan_row_budget" not in vars(backstory_gap)
    assert "configured_account_scope" not in vars(backstory_gap)
    assert "markdown_cell" not in vars(backstory_gap)


def test_gmail_package_does_not_reexport_internal_noise_policy() -> None:
    import fieldkit.gmail

    assert "NOISE_REGEX" not in vars(fieldkit.gmail)
    assert "NOISE_REGEX" not in fieldkit.gmail.__all__


def test_configured_account_scope_returns_exact_normalized_domains() -> None:
    scope = configured_account_scope(
        {
            "accounts": {"acme-corp": {"domains": ["Acme-Corp.COM", "sales.acme-corp.example.com"]}},
            "internal_domains": ["example.com"],
        },
        "acme-corp",
    )

    assert scope.key == "acme-corp"
    assert scope.domains == ("acme-corp.example.com", "sales.acme-corp.example.com")
    assert scope.internal_domains == ("example.com",)


def test_configured_account_scope_deduplicates_trailing_dot_domains() -> None:
    scope = configured_account_scope(
        {"accounts": {"acme-corp": {"domains": ["acme-corp.example.com.", "ACME-CORP.COM"]}}},
        "acme-corp",
    )

    assert scope.domains == ("acme-corp.example.com",)


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("internal.example.com", True),
        ("dept.internal.example.com", True),
        ("evilinternal.example.com", False),
        ("internal.example.com.evil.example", False),
    ],
)
def test_domain_boundary_matches_people_index_policy(domain: str, expected: bool) -> None:
    assert domain_matches_boundary(domain, ("internal.example.com",)) is expected


@pytest.mark.parametrize(
    ("config", "account"),
    [
        ({}, "acme-corp"),
        ({"accounts": []}, "acme-corp"),
        ({"accounts": {"bad_key": {"domains": ["example.com"]}}}, "bad_key"),
        ({"accounts": {"acme-corp": {"domains": []}}}, "acme-corp"),
        ({"accounts": {"acme-corp": {"domains": ["bad..example.com"]}}}, "acme-corp"),
        ({"accounts": {"acme-corp": {"domains": ["acme-corp.example.com"]}}}, "unknown"),
    ],
)
def test_configured_account_scope_rejects_invalid_or_unknown_accounts(config: dict[str, object], account: str) -> None:
    with pytest.raises(ConfigError, match=r"account|domain"):
        configured_account_scope(config, account)


@pytest.mark.parametrize(
    "config",
    [
        {
            "accounts": {
                f"account-{index}": {"domains": [f"account-{index}.example.com"]}
                for index in range(MAX_CONFIGURED_ACCOUNTS + 1)
            }
        },
        {
            "accounts": {
                "acme-corp": {
                    "domains": [
                        f"division-{index}.acme-corp.example.com" for index in range(MAX_DOMAINS_PER_ACCOUNT + 1)
                    ]
                }
            }
        },
    ],
)
def test_configured_account_scope_rejects_unbounded_configuration(config: dict[str, object]) -> None:
    with pytest.raises(ConfigError, match="too many"):
        configured_account_scope(config, "acme-corp")


def test_sqlite_query_budget_interrupts_and_restores_connection() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE values_table (value INTEGER)")
    connection.executemany("INSERT INTO values_table VALUES (?)", ((index,) for index in range(10_000)))

    with sqlite_query_budget(connection, row_budget=scan_row_budget(1), steps_per_row=1) as budget:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("SELECT COUNT(*) FROM values_table a CROSS JOIN values_table b").fetchone()
        assert budget.exhausted is True

    assert connection.execute("SELECT 1").fetchone() == (1,)
    connection.close()


@pytest.mark.parametrize("invalid_limit", [True, 1.5, "1"])
def test_scan_row_budget_rejects_non_integer_limits(invalid_limit: object) -> None:
    with pytest.raises(ValueError, match="between 1 and 500"):
        scan_row_budget(invalid_limit)  # type: ignore[arg-type]
