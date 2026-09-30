"""Read-tier quota access cannot grant quota configuration writes."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fieldkit.companion.gate import NO_ACT_POLICY, is_allowed
from fieldkit.companion.runner import run_action

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("tier", ["read", "propose", "act"])
@pytest.mark.parametrize(
    "tokens",
    [
        ["--set", "100", "--period", "2026-H2"],
        ["--set=100", "--period=2026-H2"],
        ["--period", "2026-H2"],
        ["--data-root", "other-workspace"],
        ["--future-write-option"],
    ],
)
def test_quota_read_policy_rejects_write_and_unknown_options(tokens: list[str], tier: str) -> None:
    result = is_allowed(["pipeline", "quota", *tokens], tier, NO_ACT_POLICY)

    assert result is False


@pytest.mark.parametrize("tokens", [[], ["--json"], ["--account", "acme", "--source", "pursuits"]])
def test_quota_read_policy_retains_known_read_invocations(tokens: list[str]) -> None:
    result = is_allowed(["pipeline", "quota", *tokens], "read", NO_ACT_POLICY)

    assert result is True


def test_denied_quota_mutation_never_executes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spawn = MagicMock()
    monkeypatch.setattr("fieldkit.companion.runner.subprocess.run", spawn)

    result = run_action(
        ["pipeline", "quota", "--set", "100", "--period", "2026-H2"],
        tier="read",
        policy=NO_ACT_POLICY,
        data_path=tmp_path,
        journal=False,
    )

    assert result.denied is True
    assert result.exit_code == 3
    spawn.assert_not_called()
    assert list(tmp_path.iterdir()) == []
